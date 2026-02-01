import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.io_utils import log_openai_call
from core.webdriver_setup import setup_driver
from core.analyzer import run_axe_analysis
from core.screenshot_handler import take_screenshots, create_screenshot_summary

ANGULAR_CONFIG_FILE = "angular.json"

# Flag para activar/desactivar las correcciones AUTOMÁTICAS de contraste en Angular.
# Antes de introducir estas correcciones automáticas, el flujo de Angular dependía
# casi exclusivamente del LLM y funcionaba de forma más predecible.
# Para evitar regresiones (por ejemplo, añadir siempre `color: #000000` en textos
# que están sobre fondos oscuros), las desactivamos por defecto.
ENABLE_AUTOMATIC_CONTRAST_FIXES = False


def _normalize_angular_html(html: str) -> str:
    """
    Normaliza HTML generado por Angular para poder compararlo con los templates.

    - Elimina atributos generados en runtime (_ngcontent-*, _nghost-*, ng-reflect-*, etc.)
    - Colapsa espacios en blanco para hacer comparaciones más robustas.
    """
    if not html:
        return ""

    import re

    text = html
    # Quitar atributos "ruido" típicos de Angular en el DOM renderizado
    text = re.sub(r'\s(?:_ngcontent-[^= ]*|_nghost-[^= ]*|ng-reflect-[\w-]+)="[^"]*"', "", text)
    # Normalizar espacios en blanco
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def run_axe_on_angular_app(base_url: str, run_path: str, suffix: str = "") -> Dict:
    """
    Ejecuta Axe sobre una aplicación Angular ya levantada (por ejemplo en http://localhost:4200/)
    y guarda el informe en JSON dentro del directorio de resultados de la ejecución.
    
    Args:
        base_url: URL base donde está sirviendo la app Angular (ej. http://localhost:4200/).
        run_path: Directorio de resultados de la ejecución actual.
        suffix: Sufijo opcional para diferenciar informes (ej. "_before", "_after").
    
    NOTA IMPORTANTE:
    - Esta función asume que el proyecto Angular ya está sirviendo la aplicación
      (por ejemplo, con `ng serve` o `npm start`) en la URL indicada en `base_url`.
    - No modifica ningún fichero del proyecto; solo devuelve y guarda los resultados de Axe.
    """
    safe_suffix = suffix or ""
    report_path = Path(run_path) / f"angular_axe_report{safe_suffix}.json"

    driver = None
    try:
        print(f"\n[Angular + Axe] Analizando accesibilidad en {base_url} ...")
        driver = setup_driver()
        axe_results = run_axe_analysis(
            driver,
            base_url,
            enable_dynamic_interactions=True,
            custom_interactions=None,
        )

        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(axe_results, f, indent=2, ensure_ascii=False)

        print(f"[Angular + Axe] Informe guardado en: {report_path}")
        return axe_results
    except Exception as e:
        print(f"[Angular + Axe] Error ejecutando Axe: {e}")
        raise
    finally:
        if driver:
            print("[Angular + Axe] Cerrando WebDriver.")
            driver.quit()


def map_axe_violations_to_templates(
    axe_results: Dict, project_root: Path, source_roots: Optional[List[Path]] = None
) -> Dict[str, List[Dict]]:
    """
    Mapea las violaciones de Axe (sobre HTML renderizado) a los templates Angular (*.component.html).

    Estrategia inicial (simple pero efectiva en muchos casos):
    - Para cada nodo con violación, usamos el fragmento HTML (`html`) que devuelve Axe.
    - Normalizamos tanto ese fragmento como el contenido de los templates para ignorar
      atributos dinámicos de Angular (_ngcontent-*, _nghost-*, etc.).
    - Buscamos coincidencias por substring; si encontramos el fragmento en un template,
      asociamos esa violación a ese archivo.

    Devuelve:
        Dict[str, List[Dict]] donde la clave es la ruta del template (relativa a project_root)
        y el valor es una lista de diccionarios con información de la violación y el nodo.
    """
    if not axe_results:
        return {}

    violations = axe_results.get("violations", []) or []
    if not violations:
        return {}

    # Determinar source_roots si no se pasan explícitamente
    if source_roots is None:
        angular_config = project_root / ANGULAR_CONFIG_FILE
        if angular_config.exists():
            config_data = _load_angular_config(angular_config)
            source_roots = _resolve_source_roots(project_root, config_data)
        else:
            # Fallback: buscar en ubicaciones comunes
            possible_roots = [
                project_root / "src",
                project_root / "app",
                project_root,
            ]
            source_roots = [r for r in possible_roots if r.exists()]
            if not source_roots:
                print(f"[Angular + Axe] ⚠️ No se encontró angular.json ni directorios comunes (src/, app/)")
                print(f"[Angular + Axe] Buscando templates en todo el proyecto...")
                source_roots = [project_root]

    # Cargar todos los templates en memoria:
    #   ruta relativa -> {"normalized": str, "raw": str}
    templates: Dict[str, Dict[str, str]] = {}
    for root in source_roots:
        # Incluir templates de componentes (*.component.html)
        for tpl_path in root.glob("**/*.component.html"):
            try:
                raw = tpl_path.read_text(encoding="utf-8")
                normalized = _normalize_angular_html(raw)
                rel = str(tpl_path.relative_to(project_root))
                templates[rel] = {"normalized": normalized, "raw": raw}
            except Exception:
                continue

        # Incluir también templates INLINE en ficheros TypeScript (@Component({ template: `...` }))
        for ts_path in root.glob("**/*.component.ts"):
            try:
                ts_raw = ts_path.read_text(encoding="utf-8")
            except Exception:
                continue

            import re

            # Buscar template: ` ... ` dentro de @Component({ ... })
            # Patrón simple pero efectivo: template: `...`
            inline_matches = re.findall(
                r"template\s*:\s*`([\s\S]*?)`",
                ts_raw,
                flags=re.MULTILINE,
            )
            if not inline_matches:
                continue

            for idx, inline_tpl in enumerate(inline_matches, start=1):
                normalized = _normalize_angular_html(inline_tpl)
                # Usar un nombre "virtual" para este template inline, ligado al .ts
                rel = str(ts_path.relative_to(project_root)) + f"::inline_template_{idx}"
                templates[rel] = {"normalized": normalized, "raw": inline_tpl}
    
    # Debug: mostrar cuántos templates se encontraron
    if not templates:
        print(f"[Angular + Axe] ⚠️ No se encontraron templates (*.component.html) en:")
        for root in source_roots:
            print(f"  - {root}")
        print(f"[Angular + Axe] Buscando en todo el proyecto...")
        # Búsqueda más agresiva: buscar en todo el proyecto
        for tpl_path in project_root.rglob("*.component.html"):
            try:
                raw = tpl_path.read_text(encoding="utf-8")
                normalized = _normalize_angular_html(raw)
                rel = str(tpl_path.relative_to(project_root))
                templates[rel] = {"normalized": normalized, "raw": raw}
            except Exception:
                continue
    
    if templates:
        print(f"[Angular + Axe] ✓ Encontrados {len(templates)} template(s) para mapear violaciones")
    else:
        print(f"[Angular + Axe] ⚠️ No se encontraron templates. El mapeo puede fallar.")
    
    # También incluir index.html y otros archivos HTML estáticos en src/
    src_dir = project_root / "src"
    if src_dir.exists():
        # Buscar index.html
        index_html = src_dir / "index.html"
        if index_html.exists():
            try:
                raw = index_html.read_text(encoding="utf-8")
                normalized = _normalize_angular_html(raw)
                rel = str(index_html.relative_to(project_root))
                templates[rel] = {"normalized": normalized, "raw": raw}
            except Exception:
                pass
        
        # Buscar otros archivos HTML estáticos (no componentes)
        for html_path in src_dir.rglob("*.html"):
            # Excluir componentes (ya procesados) y archivos en node_modules
            if "node_modules" in str(html_path) or html_path.name.endswith(".component.html"):
                continue
            if html_path == index_html:  # Ya procesado
                continue
            try:
                raw = html_path.read_text(encoding="utf-8")
                normalized = _normalize_angular_html(raw)
                rel = str(html_path.relative_to(project_root))
                templates[rel] = {"normalized": normalized, "raw": raw}
            except Exception:
                continue

    issues_by_template: Dict[str, List[Dict]] = {}

    for violation in violations:
        violation_id = violation.get("id", "")
        for node in violation.get("nodes", []):
            html_snippet = node.get("html") or ""
            if not html_snippet:
                continue

            normalized_snippet = _normalize_angular_html(html_snippet)
            if not normalized_snippet.strip():
                continue

            matched_template = None

            # 1) Búsqueda sobre HTML normalizado
            for rel_path, tpl_data in templates.items():
                if normalized_snippet in tpl_data["normalized"]:
                    # VALIDACIÓN: Verificar que el elemento principal del snippet esté realmente en el template
                    snippet_tag = re.search(r'<(\w+)', html_snippet)
                    if snippet_tag:
                        tag_name = snippet_tag.group(1)
                        if f'<{tag_name}' in tpl_data["raw"] or f'<{tag_name} ' in tpl_data["raw"]:
                            matched_template = rel_path
                            break

            # 2) Fallback: intentar con el fragmento original (sin normalizar)
            if not matched_template:
                raw_snippet = html_snippet.strip()
                for rel_path, tpl_data in templates.items():
                    if raw_snippet and raw_snippet in tpl_data["raw"]:
                        # VALIDACIÓN: Verificar que el elemento principal esté en el template
                        snippet_tag = re.search(r'<(\w+)', raw_snippet)
                        if snippet_tag:
                            tag_name = snippet_tag.group(1)
                            if f'<{tag_name}' in tpl_data["raw"] or f'<{tag_name} ' in tpl_data["raw"]:
                                matched_template = rel_path
                                break

            # 3) Paso extra: intentar usar el selector CSS de Axe (clases/ids) para localizar el template
            if not matched_template:
                targets = node.get("target") or []
                selector = targets[0] if targets and isinstance(targets[0], str) else None

                if selector:
                    import re

                    # Caso especial: errores en elementos raíz como <html>
                    if selector == "html" and violation_id == "html-has-lang":
                        # Buscar index.html específicamente
                        for rel_path in templates.keys():
                            if "index.html" in rel_path:
                                matched_template = rel_path
                                break
                        if matched_template:
                            # Continuar con el siguiente paso para añadir la entrada
                            pass
                    
                    if not matched_template:
                        classes = re.findall(r"\.([a-zA-Z0-9_-]+)", selector)
                        ids = re.findall(r"#([a-zA-Z0-9_-]+)", selector)
                        # También buscar nombres de elementos (sin punto ni #)
                        element_names = re.findall(r"^([a-zA-Z][a-zA-Z0-9-]*)(?=[\.#\s>+~:\[\]()]|$)", selector)

                        candidate_paths = []
                        for rel_path, tpl_data in templates.items():
                            raw_tpl = tpl_data["raw"]

                            # Buscar por nombres de elementos (ej: "html", "body", "nb-icon")
                            if element_names:
                                element_found = False
                                for elem_name in element_names:
                                    # Buscar el elemento en el template (puede tener atributos)
                                    if f"<{elem_name}" in raw_tpl or f"<{elem_name} " in raw_tpl or f"<{elem_name}>" in raw_tpl:
                                        element_found = True
                                        break
                                if not element_found:
                                    continue

                            # Todas las clases del selector deben aparecer en el template
                            if classes and not all(cls in raw_tpl for cls in classes):
                                continue

                            # Todos los ids del selector deben aparecer en el template
                            if ids:
                                has_all_ids = True
                                for id_value in ids:
                                    if (
                                        f'id="{id_value}"' not in raw_tpl
                                        and f"id='{id_value}'" not in raw_tpl
                                    ):
                                        has_all_ids = False
                                        break
                                if not has_all_ids:
                                    continue

                            if classes or ids or element_names:
                                candidate_paths.append(rel_path)

                        # Si solo hay un candidato claro, lo usamos
                        if len(candidate_paths) == 1:
                            matched_template = candidate_paths[0]
                        # Si hay múltiples candidatos pero uno es index.html y el error es html-has-lang, usar index.html
                        elif len(candidate_paths) > 1 and violation_id == "html-has-lang":
                            for rel_path in candidate_paths:
                                if "index.html" in rel_path:
                                    matched_template = rel_path
                                    break
                        # Si hay múltiples candidatos y no es un caso especial, asociar la violación a TODOS
                        elif len(candidate_paths) > 1:
                            for rel_path in candidate_paths:
                                entry = {
                                    "violation_id": violation_id,
                                    "violation": violation,
                                    "node": node,
                                }
                                issues_by_template.setdefault(rel_path, []).append(entry)
                            # Ya hemos asignado esta violación a varios templates, continuar con el siguiente nodo
                            continue

            if not matched_template:
                continue

            entry = {
                "violation_id": violation_id,
                "violation": violation,
                "node": node,
            }
            issues_by_template.setdefault(matched_template, []).append(entry)

    return issues_by_template


def fix_css_with_axe(
    axe_results: Dict, project_root: Path, client
) -> Dict[str, Dict[str, str]]:
    """
    Aplica correcciones de contraste basadas en Axe a nivel de CSS global.

    Versión inicial y conservadora:
    - Solo actúa sobre violaciones 'color-contrast'.
    - Solo considera selectores sencillos de clase (ej: '.navbar-brand').
    - Solo genera reglas CSS nuevas para esos selectores y las añade al final
      de 'src/styles.scss' (o 'src/styles.css' si no existe el primero).
    - No toca layout (display, flex, grid, etc.), solo color / background-color
      y opcionalmente font-weight.
    """
    fixes: Dict[str, Dict[str, str]] = {}

    if not axe_results:
        return fixes

    violations = axe_results.get("violations", []) or []
    if not violations:
        return fixes

    # Localizar hoja de estilos global principal
    styles_scss = project_root / "src" / "styles.scss"
    styles_css = project_root / "src" / "styles.css"
    if styles_scss.exists():
        styles_path = styles_scss
    elif styles_css.exists():
        styles_path = styles_css
    else:
        # No hay estilos globales estándar, salir sin hacer nada
        return fixes

    try:
        original_styles = styles_path.read_text(encoding="utf-8")
    except Exception:
        return fixes

    # Agrupar violaciones de contraste por selector simple (clase)
    from collections import defaultdict
    import re

    issues_by_selector: Dict[str, List[Dict]] = defaultdict(list)
    
    # Selectores demasiado genéricos que NO debemos usar (romperían el diseño)
    GENERIC_SELECTORS_BLACKLIST = {
        ".btn", ".container", ".row", ".col", ".card", ".nav", ".navbar",
        ".form", ".input", ".label", ".text", ".title", ".header", ".footer",
        ".main", ".content", ".wrapper", ".section", ".div", ".span", ".p",
        ".a", ".button", ".img", ".ul", ".li", ".table", ".tr", ".td"
    }

    for violation in violations:
        if violation.get("id") != "color-contrast":
            continue
        for node in violation.get("nodes", []):
            # Intentar derivar un selector CSS basado en la clase del elemento
            html = node.get("html") or ""
            targets = node.get("target") or []

            selector = None

            # 1) Extraer TODAS las clases del HTML y elegir la MÁS ESPECÍFICA (no la primera)
            class_match = re.search(r'class=["\']([^"\']+)["\']', html)
            if class_match:
                classes_in_html = class_match.group(1).split()
                if classes_in_html:
                    # Priorizar clases más específicas (que no estén en blacklist)
                    # Ej: "btn btn-primary" -> preferir ".btn-primary" sobre ".btn"
                    for cls in reversed(classes_in_html):  # Empezar por la última (más específica)
                        candidate = f".{cls}"
                        if candidate not in GENERIC_SELECTORS_BLACKLIST:
                            selector = candidate
                            break
                    # Si todas están en blacklist, usar la última de todas formas (mejor que nada)
                    if not selector and classes_in_html:
                        selector = f".{classes_in_html[-1]}"

            # 2) Si no hay clase en el HTML, usar el target de Axe si es una clase simple
            if not selector and targets and isinstance(targets[0], str):
                raw_selector = targets[0].strip()
                # Extraer solo la parte de clase del selector (ignorar atributos, pseudo-clases, etc.)
                class_parts = re.findall(r'\.([a-zA-Z0-9_-]+)', raw_selector)
                if class_parts:
                    # Usar la última clase encontrada (más específica)
                    selector = f".{class_parts[-1]}"
                    if selector in GENERIC_SELECTORS_BLACKLIST:
                        # Si es genérica, intentar con la anterior
                        if len(class_parts) > 1:
                            selector = f".{class_parts[-2]}"
                        else:
                            selector = None  # Descartar si solo hay una clase genérica

            if not selector or selector in GENERIC_SELECTORS_BLACKLIST:
                continue

            # Extraer datos de contraste de la primera entrada relevante
            contrast_data = None
            any_checks = node.get("any", []) or []
            for check in any_checks:
                data = check.get("data")
                if isinstance(data, dict) and data.get("bgColor") and data.get("fgColor"):
                    contrast_data = data
                    break

            issues_by_selector[selector].append(
                {
                    "violation": violation,
                    "node": node,
                    "contrast": contrast_data,
                }
            )

    if not issues_by_selector:
        return fixes

    updated_css_blocks: List[str] = []

    for selector, issues in issues_by_selector.items():
        # Construir texto de problemas para el prompt
        problems_lines: List[str] = []
        for issue in issues:
            data = (issue.get("contrast") or {}) if issue.get("contrast") else {}
            bg = data.get("bgColor")
            fg = data.get("fgColor")
            ratio = data.get("contrastRatio")
            expected = data.get("expectedContrastRatio")
            problems_lines.append(
                f"- Selector: {selector} | bgColor: {bg} | fgColor: {fg} | "
                f"ratio: {ratio} | ratio requerido: {expected}"
            )

        problems_text = "\n".join(problems_lines)

        # Verificar si ya existe una regla para este selector (evitar duplicados)
        selector_exists = re.search(rf'\.{re.escape(selector.lstrip("."))}\s*\{{', original_styles, re.IGNORECASE)
        existing_note = ""
        if selector_exists:
            existing_note = f"\n⚠️ IMPORTANTE: Ya existe una regla para {selector} en el CSS. Tu nueva regla DEBE usar !important para sobrescribirla."

        prompt = f"""
Tienes un proyecto Angular con Bootstrap. Axe ha detectado ERRORES DE CONTRASTE (regla color-contrast)
para el selector CSS {selector}.

DETALLES DE LAS VIOLACIONES (PUEDEN SER VARIAS INSTANCIAS):
{problems_text}
{existing_note}

HOJA DE ESTILOS GLOBAL ACTUAL (resumen):
```css
{original_styles[:4000]}
```

TAREA CRÍTICA:
- Debes proponer nuevas reglas CSS para el selector {selector} (y solo para él) que corrijan
  TODOS los errores de contraste indicados.
- Como este proyecto usa Bootstrap, DEBES usar !important en color para
  asegurar que tus reglas sobrescriban los estilos de Bootstrap.
- 🚨 IMPORTANTE: NO uses `background-color` a menos que sea absolutamente necesario.
  Bootstrap ya maneja los fondos correctamente. Solo ajusta el `color` del texto.
- NO CAMBIES el layout: NO toques display, position, flex, grid, width, height,
  margin, padding, align-items, justify-content, etc.
- SOLO PUEDES MODIFICAR O AÑADIR:
  - color (con !important) - OBLIGATORIO
  - font-weight (opcional, solo si realmente ayuda a la legibilidad)
- Calcula colores que cumplan al menos el ratio requerido (4.5:1 para texto normal, 3:1 para texto grande).
- Para fondos oscuros (#007bff, #17a2b8, etc.), usa texto claro (#ffffff o similar).
- Para fondos claros, usa texto oscuro (#000000, #212121, etc.).

FORMATO DE RESPUESTA OBLIGATORIO:
Devuelve EXCLUSIVAMENTE un bloque CSS listo para PEGAR al final de styles.css/styles.scss,
DELIMITADO por:

<<<UPDATED_CSS>>>
{selector} {{
  color: #XXXXXX !important;
}}
<<<END_UPDATED_CSS>>>

NOTA: Solo incluye `color`, NO incluyas `background-color` a menos que sea absolutamente crítico.

NO incluyas explicaciones, ni markdown, ni ```css```, solo el bloque entre los marcadores.
""".strip()

        system_message = (
            "Eres un experto en accesibilidad (WCAG 2.2 AA) y en CSS. "
            "Tu tarea es ajustar colores de texto/fondo para mejorar el contraste "
            "SIN alterar el layout ni romper el diseño general."
        )

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )
            content = response.choices[0].message.content or ""
            log_openai_call(
                prompt=prompt,
                response=content,
                model="gpt-4o",
                call_type="angular_axe_css_fix",
            )

            # Extraer bloque UPDATED_CSS
            start_marker = "<<<UPDATED_CSS>>>"
            end_marker = "<<<END_UPDATED_CSS>>>"
            start_idx = content.find(start_marker)
            end_idx = content.find(end_marker)
            if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
                continue

            updated_block = content[start_idx + len(start_marker) : end_idx].strip()
            if not updated_block:
                continue

            # Validación muy básica: evitar propiedades de layout peligrosas
            forbidden_props = [
                "display:",
                "position:",
                "flex:",
                "grid:",
                "width:",
                "height:",
                "margin:",
                "padding:",
                "top:",
                "left:",
                "right:",
                "bottom:",
            ]
            lower_block = updated_block.lower()
            if any(prop in lower_block for prop in forbidden_props):
                continue

            updated_css_blocks.append(
                f"/* Axe-based contrast fix para {selector} */\n{updated_block}\n"
            )

        except Exception as e:
            print(f"[Angular + Axe CSS] ⚠️ Error corrigiendo selector {selector}: {e}")
            continue

    if not updated_css_blocks:
        return fixes

    # Limpiar reglas antiguas de "Axe-based contrast fix" para evitar acumulación
    # Usar regex para eliminar bloques que empiezan con "/* Axe-based contrast fix" hasta el siguiente bloque o fin
    axe_block_pattern = r'/\* Axe-based contrast fix para[^*]*\*/(?:[^*]|\*(?!/))*?}'
    cleaned_styles = re.sub(axe_block_pattern, '', original_styles, flags=re.DOTALL)
    # Limpiar líneas en blanco múltiples
    cleaned_styles = re.sub(r'\n\s*\n\s*\n+', '\n\n', cleaned_styles).rstrip()

    new_styles = cleaned_styles + "\n\n" + "\n\n".join(updated_css_blocks) + "\n"
    if new_styles != original_styles:
        try:
            styles_path.write_text(new_styles, encoding="utf-8")
            fixes[str(styles_path)] = {
                "original": original_styles,
                "corrected": new_styles,
            }
            print(
                f"[Angular + Axe CSS] ✓ Añadidas {len(updated_css_blocks)} reglas de contraste en {styles_path}"
            )
        except Exception as e:
            print(f"[Angular + Axe CSS] ⚠️ No se pudo escribir en {styles_path}: {e}")

    return fixes


def _build_axe_based_prompt_for_template(
    template_path: str, template_content: str, issues: List[Dict]
) -> str:
    """
    Prompt compacto para corregir accesibilidad en un template Angular
    a partir de las violaciones de Axe.
    """
    violations_lines: List[str] = []

    for issue in issues:
        violation = issue.get("violation", {}) or {}
        node = issue.get("node", {}) or {}

        v_id = violation.get("id", "unknown")
        impact = violation.get("impact", "moderate")
        desc = violation.get("description", "")
        html_snippet = (node.get("html") or "").strip()

        # Tag principal del snippet (para que el modelo sepa qué buscar)
        tag = "elemento"
        m = re.search(r"<(\w+)", html_snippet)
        if m:
            tag = m.group(1)

        # Línea principal de la violación
        line = f"- {v_id} ({impact}) en <{tag}>"
        if desc:
            line += f": {desc}"
        violations_lines.append(line)

        # Añadir una sola línea de HTML para referencia
        if html_snippet:
            first_line = html_snippet.splitlines()[0].strip()
            violations_lines.append(f"  HTML: {first_line[:200]}...")

    violations_text = "\n".join(violations_lines)
    total = len(issues)

    prompt = f"""Corrige TODAS las {total} violaciones WCAG A/AA en este template Angular.

TEMPLATE: {template_path}

VIOLACIONES:
{violations_text}

REGLAS RÁPIDAS:
- button-name → añade texto visible o aria-label="..." a <button>
- color-contrast → ajusta SOLO style="color:#000000" o "#FFFFFF" según el fondo
- link-name → añade texto descriptivo o aria-label="..." a <a>
- image-alt / role-img-alt → añade alt="..." o aria-label="..." al elemento visual
- frame-title → añade title="..." a <iframe>
- aria-* → añade/corrige atributos aria- (aria-label, aria-labelledby, etc.)

INSTRUCCIONES:
- Corrige SOLO los elementos indicados en la lista de violaciones.
- Mantén *ngIf, *ngFor, bindings y pipes sin romperlos.
- No cambies el layout ni las clases de responsive (row, col-*, container, etc.).
- No añadas elementos HTML nuevos innecesarios; prioriza atributos en elementos existentes.

TEMPLATE COMPLETO ACTUAL:
```html
{template_content}
```

Devuelve SOLO el template completo corregido, sin explicaciones."""

    return prompt.strip()


def fix_templates_with_axe_violations(
    issues_by_template: Dict[str, List[Dict]], project_root: Path, client
) -> Dict[str, Dict[str, str]]:
    """
    Usa la información de Axe ya mapeada a cada template para pedir al LLM que
    corrija el HTML completo de cada *.component.html.

    Devuelve un dict con:
      { template_rel_path: { "original": ..., "corrected": ... }, ... }
    """
    import re
    fixes: Dict[str, Dict[str, str]] = {}

    if not issues_by_template:
        print("[Angular + Axe] No hay violaciones mapeadas a templates.")
        return fixes

    for rel_path, issues in issues_by_template.items():
        try:
            # Soportar tanto templates en archivos HTML como templates INLINE en .ts
            ts_inline_suffix = "::inline_template_"
            is_inline = ts_inline_suffix in rel_path

            if is_inline:
                # Ejemplo de rel_path:
                #   "src/app/components/ng-style/ng-style.component.ts::inline_template_1"
                ts_rel, inline_id = rel_path.split(ts_inline_suffix, 1)
                tpl_path = project_root / ts_rel
                if not tpl_path.exists():
                    continue
                ts_content = tpl_path.read_text(encoding="utf-8")

                # Volver a localizar todas las ocurrencias de template: ` ... `
                inline_matches = list(
                    re.finditer(
                        r"template\s*:\s*`([\s\S]*?)`",
                        ts_content,
                        flags=re.MULTILINE,
                    )
                )
                if not inline_matches:
                    continue

                # Calcular índice de template inline (1-based en el nombre virtual)
                try:
                    target_idx = int(inline_id)
                except ValueError:
                    target_idx = 1

                if target_idx < 1 or target_idx > len(inline_matches):
                    continue

                match = inline_matches[target_idx - 1]
                original_content = match.group(1)
            else:
                tpl_path = project_root / rel_path
                if not tpl_path.exists():
                    continue

                original_content = tpl_path.read_text(encoding="utf-8")

            if not original_content.strip():
                continue

            # VALIDACIÓN CRÍTICA: Verificar que las violaciones realmente corresponden a este template
            print(f"[Angular + Axe] 🔍 Validando mapeo de violaciones para {rel_path}...")
            valid_issues = []
            invalid_issues = []
            
            for issue in issues:
                violation = issue.get("violation", {})
                node = issue.get("node", {})
                html_snippet = (node.get("html") or "").strip()
                violation_id = violation.get("id", "unknown")
                is_valid = True
                
                if html_snippet:
                    # Extraer el tag principal del snippet
                    snippet_tag_match = re.search(r'<(\w+)', html_snippet)
                    if snippet_tag_match:
                        snippet_tag = snippet_tag_match.group(1)
                        # Verificar que el tag esté en el template
                        if snippet_tag not in ['html', 'body', 'head']:  # Excluir tags raíz
                            if f'<{snippet_tag}' not in original_content and f'<{snippet_tag} ' not in original_content:
                                print(f"[Angular + Axe] ⚠️ Violación {violation_id} tiene elemento <{snippet_tag}> que NO está en este template")
                                print(f"  → HTML snippet: {html_snippet[:150]}...")
                                print(f"  → Esta violación se OMITIRÁ porque el mapeo parece incorrecto")
                                is_valid = False
                
                if is_valid:
                    valid_issues.append(issue)
                else:
                    invalid_issues.append(issue)
            
            if invalid_issues:
                print(f"[Angular + Axe] ⚠️ Se omitieron {len(invalid_issues)} violación(es) con mapeo incorrecto")
            
            if not valid_issues:
                print(f"[Angular + Axe] ⚠️ No hay violaciones válidas para corregir en {rel_path}. Saltando...")
                continue
            
            # Usar solo las violaciones válidas
            issues = valid_issues
            print(f"[Angular + Axe] ✓ {len(issues)} violación(es) válida(s) para corregir en {rel_path}")
            
            prompt = _build_axe_based_prompt_for_template(
                rel_path, original_content, issues
            )

            system_message = (
                "Eres un EXPERTO en accesibilidad web (WCAG 2.2 A+AA) y Angular. "
                "Tu MISIÓN es corregir TODAS las violaciones de accesibilidad indicadas por Axe "
                "modificando el template HTML completo. "
                "🚨 CRÍTICO: DEBES hacer cambios reales al código. NO devuelvas el mismo código. "
                "🚨 Si hay violaciones de contraste, DEBES añadir o modificar style=\"color: #XXXXXX;\" "
                "🚨 Si hay violaciones de aria-label, button-name, link-name, etc., DEBES añadir los atributos necesarios. "
                "🚨 Mantén la lógica Angular (bindings, *ngIf, *ngFor, pipes) sin romperla. "
                "🚨 Si devuelves el mismo código sin cambios, la corrección FALLA completamente."
            )

            print(f"[Angular + Axe] Corrigiendo template basado en Axe: {rel_path}")
            
            # Log del prompt para debugging (primeros 1000 chars)
            print(f"[Angular + Axe] 📝 Prompt (primeros 1000 chars): {prompt[:1000]}...")
            print(f"[Angular + Axe] 📄 Código original (primeros 500 chars): {original_content[:500]}...")

            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )

            corrected = response.choices[0].message.content or ""
            
            # Log de la respuesta del LLM (primeros 500 chars)
            print(f"[Angular + Axe] 📝 Respuesta LLM (primeros 500 chars): {corrected[:500]}...")
            
            log_openai_call(
                prompt=prompt,
                response=corrected,
                model="gpt-4o",
                call_type="angular_axe_template_fix",
            )

            # Limpiar posibles marcas de bloque de código
            corrected = corrected.strip()
            if corrected.startswith("```"):
                parts = corrected.split("```")
                if len(parts) >= 3:
                    code_block = parts[1]
                    # Quitar posibles etiquetas de lenguaje
                    if "\n" in code_block:
                        code_block = code_block.split("\n", 1)[1]
                    corrected = code_block.strip()
                else:
                    corrected = corrected.replace("```html", "").replace("```", "").strip()

            # Aplicar correcciones automáticas post-procesamiento
            corrected = _apply_automatic_accessibility_fixes(corrected)
            
            # Corregir errores básicos de sintaxis
            corrected = _fix_basic_syntax_errors(corrected)
            
            # Corregir sintaxis Angular para atributos ARIA
            corrected = _fix_angular_aria_syntax(corrected)

            # VALIDACIÓN CRÍTICA: Verificar que el LLM devolvió HTML válido
            is_valid_response = True
            
            # 1. No debe ser un comentario o texto sin HTML
            if corrected.strip().startswith("//") or corrected.strip().startswith("/*"):
                print(f"[Angular + Axe] ⚠️ El LLM devolvió un comentario en lugar de HTML para {rel_path}")
                is_valid_response = False
            
            # 2. Debe contener al menos una etiqueta HTML
            if is_valid_response and not re.search(r'<\w+', corrected):
                print(f"[Angular + Axe] ⚠️ El LLM no devolvió HTML válido para {rel_path}")
                is_valid_response = False
            
            # 3. No debe ser significativamente más corto que el original (más del 50% más corto)
            if is_valid_response and len(corrected.strip()) < len(original_content.strip()) * 0.5:
                print(f"[Angular + Axe] ⚠️ La respuesta del LLM es demasiado corta para {rel_path} ({len(corrected)} vs {len(original_content)} chars)")
                is_valid_response = False

            # Detectar diferencias más robustamente (incluyendo cambios de color)
            orig_colors = re.findall(r'color\s*:\s*["\']?([^"\';]+)', original_content, re.IGNORECASE)
            corr_colors = re.findall(r'color\s*:\s*["\']?([^"\';]+)', corrected, re.IGNORECASE) if corrected else []
            has_color_diff = set(orig_colors) != set(corr_colors)
            
            # Comparación más robusta: normalizar espacios pero detectar cambios reales
            orig_normalized = re.sub(r'\s+', ' ', original_content.strip())
            corr_normalized = re.sub(r'\s+', ' ', corrected.strip()) if corrected else ""
            
            # Detectar cambios en atributos ARIA, alt, aria-label, etc.
            orig_aria = set(re.findall(r'aria-\w+="[^"]*"', original_content, re.IGNORECASE))
            corr_aria = set(re.findall(r'aria-\w+="[^"]*"', corrected, re.IGNORECASE)) if corrected else set()
            has_aria_diff = orig_aria != corr_aria
            
            orig_alt = set(re.findall(r'alt="[^"]*"', original_content, re.IGNORECASE))
            corr_alt = set(re.findall(r'alt="[^"]*"', corrected, re.IGNORECASE)) if corrected else set()
            has_alt_diff = orig_alt != corr_alt
            
            orig_labels = set(re.findall(r'<label[^>]*>', original_content, re.IGNORECASE))
            corr_labels = set(re.findall(r'<label[^>]*>', corrected, re.IGNORECASE)) if corrected else set()
            has_label_diff = orig_labels != corr_labels
            
            has_changes = (
                orig_normalized != corr_normalized or 
                has_color_diff or
                has_aria_diff or
                has_alt_diff or
                has_label_diff or
                corrected.strip() != original_content.strip()
            )
            
            # Debug: mostrar si hay cambios
            print(f"[Angular + Axe] 🔍 Análisis de cambios:")
            print(f"  - Código normalizado igual: {orig_normalized == corr_normalized}")
            print(f"  - Diferencia de color: {has_color_diff} (orig: {orig_colors}, corr: {corr_colors})")
            print(f"  - Diferencia de ARIA: {has_aria_diff} (orig: {len(orig_aria)}, corr: {len(corr_aria)})")
            print(f"  - Diferencia de alt: {has_alt_diff} (orig: {len(orig_alt)}, corr: {len(corr_alt)})")
            print(f"  - Diferencia de labels: {has_label_diff} (orig: {len(orig_labels)}, corr: {len(corr_labels)})")
            print(f"  - Tiene cambios: {has_changes}")
            
            if not has_changes:
                print(f"[Angular + Axe] ⚠️ NO SE DETECTARON CAMBIOS - Comparación detallada:")
                print(f"  - Original (primeros 300): {original_content[:300]}")
                print(f"  - Corregido (primeros 300): {corrected[:300] if corrected else 'N/A'}")
                print(f"  - Longitud original: {len(original_content)}")
                print(f"  - Longitud corregido: {len(corrected) if corrected else 0}")
            
            if is_valid_response and corrected and has_changes:
                if has_color_diff:
                    print(f"[Angular + Axe] 🎨 Diferencia en colores detectada: {orig_colors} -> {corr_colors}")
                if is_inline:
                    # Reemplazar solo el contenido del template inline dentro del .ts
                    before = ts_content[: match.start(1)]
                    after = ts_content[match.end(1) :]

                    # Escapar backticks dentro del template corregido
                    safe_corrected = corrected.replace("`", "\\`")

                    new_ts_content = before + safe_corrected + after
                    if new_ts_content != ts_content:
                        try:
                            tpl_path.write_text(new_ts_content, encoding="utf-8")
                            # Verificar que se escribió correctamente
                            written_content = tpl_path.read_text(encoding="utf-8")
                            if written_content.strip() == new_ts_content.strip():
                                fixes[rel_path] = {
                                    "original": original_content,
                                    "corrected": corrected,
                                }
                                print(
                                    f"[Angular + Axe] ✓ Cambios aplicados y verificados en template inline de {rel_path}"
                                )
                                print(f"  → Longitud original: {len(original_content)} chars")
                                print(f"  → Longitud corregido: {len(corrected)} chars")
                            else:
                                print(
                                    f"[Angular + Axe] ⚠️ Error: El archivo no se escribió correctamente en template inline de {rel_path}"
                                )
                        except Exception as e:
                            print(f"[Angular + Axe] ⚠️ Error escribiendo archivo {rel_path}: {e}")
                    else:
                        print(
                            f"[Angular + Axe] ⚠️ No se aplicaron cambios efectivos en template inline de {rel_path}"
                        )
                        print(f"  → El contenido nuevo es idéntico al original")
                        print(f"  → Original (primeros 200): {original_content[:200]}")
                        print(f"  → Corregido (primeros 200): {corrected[:200]}")
                else:
                    # Verificar que el archivo existe y es escribible
                    if not tpl_path.exists():
                        print(f"[Angular + Axe] ⚠️ El archivo {tpl_path} no existe. No se pueden aplicar cambios.")
                        continue
                    
                    # Escribir el archivo
                    try:
                        tpl_path.write_text(corrected, encoding="utf-8")
                        # Verificar que se escribió correctamente
                        written_content = tpl_path.read_text(encoding="utf-8")
                        if written_content.strip() == corrected.strip():
                            fixes[rel_path] = {
                                "original": original_content,
                                "corrected": corrected,
                            }
                            print(f"[Angular + Axe] ✓ Cambios aplicados y verificados en {rel_path}")
                            print(f"  → Longitud original: {len(original_content)} chars")
                            print(f"  → Longitud corregido: {len(corrected)} chars")
                        else:
                            print(f"[Angular + Axe] ⚠️ Error: El archivo no se escribió correctamente en {rel_path}")
                    except Exception as e:
                        print(f"[Angular + Axe] ⚠️ Error escribiendo archivo {rel_path}: {e}")
            else:
                print(f"[Angular + Axe] ⚠️ El LLM devolvió el mismo código para {rel_path}")
                # Mostrar qué violaciones se intentaron corregir
                violation_ids = [issue.get("violation", {}).get("id", "unknown") for issue in issues]
                print(f"  → Violaciones que se intentaron corregir: {', '.join(set(violation_ids))}")
                print(f"  → Total de violaciones: {len(issues)}")
                # Mostrar un ejemplo de HTML snippet para debugging
                if issues:
                    for i, issue in enumerate(issues[:3], 1):
                        violation = issue.get("violation", {})
                        node = issue.get("node", {})
                        html_snippet = (node.get("html") or "")[:200]
                        violation_id = violation.get("id", "unknown")
                        print(f"  → Violación {i} ({violation_id}): {html_snippet}...")
                
                # Mostrar qué debería haberse corregido
                print(f"[Angular + Axe] 💡 Qué debería haberse corregido:")
                for issue in issues:
                    violation = issue.get("violation", {})
                    violation_id = violation.get("id", "unknown")
                    if "button-name" in violation_id.lower():
                        print(f"  - Añadir aria-label o texto visible a <button>")
                    elif "color-contrast" in violation_id.lower():
                        print(f"  - Añadir/modificar style=\"color: #XXXXXX;\"")
                    elif "link-name" in violation_id.lower():
                        print(f"  - Añadir texto descriptivo o aria-label a <a>")
                    elif "aria" in violation_id.lower():
                        print(f"  - Añadir/modificar atributos aria-*")
                    elif "alt" in violation_id.lower() or "image" in violation_id.lower():
                        print(f"  - Añadir/modificar atributo alt en <img>")
                
                print(f"[Angular + Axe] ⚠️ El LLM NO aplicó las correcciones. Posibles razones:")
                print(f"  1. El elemento de la violación no está en el template (mapeo incorrecto)")
                print(f"  2. El LLM no encontró el elemento correcto en el código")
                print(f"  3. El prompt no fue lo suficientemente específico")
                print(f"  4. El LLM decidió que no necesita cambios (incorrecto)")

        except Exception as e:
            print(f"[Angular + Axe] ⚠️ Error corrigiendo {rel_path}: {e}")

    return fixes


def process_angular_project(project_path: str, client, run_path: str, serve_app: bool = False) -> List[str]:
    """
    Procesa un proyecto Angular local, detecta componentes y aplica correcciones
    de accesibilidad utilizando el LLM.

    Args:
        project_path: Ruta absoluta al proyecto Angular.
        client: Cliente OpenAI ya inicializado.
        run_path: Ruta donde se guardarán reportes y artefactos.

    Returns:
        Lista de líneas de resumen para mostrar en consola.
    """
    project_root = Path(project_path).resolve()
    if not project_root.exists():
        raise FileNotFoundError(f"La ruta {project_root} no existe.")

    angular_config = project_root / ANGULAR_CONFIG_FILE
    if not angular_config.exists():
        raise ValueError("No se detectó angular.json en el proyecto. Asegúrate de seleccionar un proyecto Angular válido.")

    config_data = _load_angular_config(angular_config)
    source_roots = _resolve_source_roots(project_root, config_data)

    if not source_roots:
        raise ValueError("No se pudo determinar el directorio de código fuente en angular.json.")

    templates = _discover_component_templates(source_roots)

    summary_lines: List[str] = []
    stats = {"templates": len(templates), "updated": 0, "errors": 0, "build_failures": 0, "compilation_fixes": 0}
    processed_components: List[Dict] = []
    changes_map: List[Dict] = []  # Mapa de cambios para aplicar después

    # FASE 1: Compilar el proyecto y capturar errores de compilación
    print("\n[Fase 1] Compilando proyecto Angular...")
    build_result = _compile_and_get_errors(project_root)
    
    # Debug: mostrar errores si los hay
    if not build_result["success"] and build_result.get("errors"):
        print(f"  → Errores detectados: {len(build_result.get('errors', []))}")
        for i, error in enumerate(build_result.get("errors", [])[:3], 1):
            print(f"    Error {i}: {error[:200]}...")
    
    if not build_result["verification_available"]:
        print("⚠️ No se pudo compilar el proyecto (ng no disponible).")
        print("  Continuando con correcciones de accesibilidad...")
    elif build_result["success"]:
        print("✓ Proyecto compila correctamente.")
    else:
        print(f"✗ El proyecto tiene {len(build_result.get('errors', []))} errores de compilación.")
        print("  Corrigiendo errores de compilación con LLM...")
        
        # Corregir errores de compilación con LLM
        compilation_fixes = _fix_compilation_errors(build_result.get("errors", []), project_root, client)
        stats["compilation_fixes"] = len(compilation_fixes)
        
        if compilation_fixes:
            print(f"  → Aplicando {len(compilation_fixes)} correcciones de compilación...")
            _apply_compilation_fixes(compilation_fixes, project_root)
            
            # Recompilar para verificar
            print("  → Recompilando después de correcciones...")
            build_result = _compile_and_get_errors(project_root)
            if build_result["success"]:
                print("  ✓ Errores de compilación corregidos exitosamente.")
            else:
                print(f"  ⚠️ Aún hay {len(build_result.get('errors', []))} errores de compilación.")
                summary_lines.append(f"⚠️ {len(build_result.get('errors', []))} errores de compilación pendientes")

    # FASE 2: Ejecutar Axe para obtener errores reales de accesibilidad
    print(f"\n[Fase 2] Ejecutando análisis de Axe para detectar errores reales...")
    axe_results = None
    issues_by_template = {}
    dev_server_process = None
    screenshot_paths = []  # Inicializar lista de capturas
    
    if serve_app:
        try:
            import time
            import socket
            from urllib.request import urlopen
            from urllib.error import URLError
            
            base_url = "http://localhost:4200"
            
            # Primero verificar si el servidor ya está corriendo
            server_running = False
            try:
                response = urlopen(base_url, timeout=2)
                server_running = True
                print(f"  → Servidor Angular ya está corriendo en {base_url}")
            except (URLError, socket.timeout):
                print(f"  → Servidor Angular no está corriendo, iniciándolo...")
                # Iniciar el servidor Angular antes de ejecutar Axe
                dev_server_process = _start_angular_dev_server(project_root, port=4200, wait_for_ready=True)
                if dev_server_process:
                    print(f"  → Esperando a que el servidor esté listo...")
                    # Esperar hasta que el servidor esté listo
                    max_wait = 120  # 2 minutos máximo
                    wait_interval = 2
                    waited = 0
                    while waited < max_wait:
                        try:
                            response = urlopen(base_url, timeout=2)
                            server_running = True
                            print(f"  ✓ Servidor Angular está listo en {base_url}")
                            break
                        except (URLError, socket.timeout):
                            time.sleep(wait_interval)
                            waited += wait_interval
                            print(f"  → Esperando... ({waited}s)")
                    
                    if not server_running:
                        print(f"  ⚠️ No se pudo conectar al servidor después de {max_wait}s")
                        print("  → Continuando con análisis estático de código...")
            
            # Ejecutar Axe si el servidor está corriendo
            if server_running:
                print("  → Ejecutando Axe en aplicación Angular...")
                try:
                    driver = setup_driver()
                    driver.get(base_url)
                    time.sleep(5)  # Esperar a que cargue completamente la página
                    
                    # TOMAR CAPTURAS DE PANTALLA AUTOMÁTICAS (antes de correcciones)
                    print("  → Tomando capturas de pantalla en diferentes tamaños...")
                    screenshots_dir = Path(run_path) / "screenshots" / "before"
                    screenshot_paths = take_screenshots(
                        driver,
                        base_url,
                        screenshots_dir,
                        prefix="before"
                    )
                    if screenshot_paths:
                        print(f"  ✓ {len(screenshot_paths)} capturas guardadas")
                        # Crear resumen HTML de las capturas
                        summary_path = screenshots_dir / "summary.html"
                        create_screenshot_summary(screenshot_paths, summary_path)
                        print(f"  ✓ Resumen visual guardado en: {summary_path}")
                        print(f"  → Las capturas se incluirán en el prompt del LLM para mejor contexto visual")
                    else:
                        screenshot_paths = []  # Asegurar que es una lista vacía
                    
                    # Ejecutar análisis de Axe
                    axe_results = run_axe_analysis(driver, base_url, is_local_file=False)
                    driver.quit()
                    
                    # Guardar las rutas de capturas para usarlas en el procesamiento de componentes
                    # (se guardará en una variable para pasar a los componentes)
                    
                    if axe_results and axe_results.get("violations"):
                        print(f"  ✓ Axe detectó {len(axe_results['violations'])} violaciones")
                        issues_by_template = map_axe_violations_to_templates(axe_results, project_root, source_roots)
                        print(f"  ✓ Errores mapeados a {len(issues_by_template)} templates")
                        
                        # Guardar reporte de Axe en el directorio de resultados
                        axe_report_path = Path(run_path) / "angular_axe_report.json"
                        with open(axe_report_path, "w", encoding="utf-8") as f:
                            json.dump(axe_results, f, indent=2, ensure_ascii=False)
                        print(f"  ✓ Reporte de Axe guardado en: {axe_report_path}")
                    else:
                        print("  ⚠️ Axe no detectó violaciones (puede que no haya errores o la página no cargó)")
                except Exception as e:
                    print(f"  ⚠️ No se pudo ejecutar Axe: {e}")
                    print("  → Continuando con análisis estático de código...")
        except Exception as e:
            print(f"  ⚠️ Error al intentar ejecutar Axe: {e}")
            print("  → Continuando con análisis estático de código...")
    else:
        print("  → Modo sin servidor: usando solo análisis estático de código")
    
    # FASE 3: Procesar componentes y generar mapa de cambios de accesibilidad (sandbox)
    print(f"\n[Fase 3] Generando mapa de cambios de accesibilidad en sandbox...")
    for template_path in templates:
        try:
            # Obtener errores de Axe para este template específico
            template_rel_path = str(template_path.relative_to(project_root))
            axe_errors_for_template = issues_by_template.get(template_rel_path, [])
            
            # Obtener rutas de capturas de pantalla si están disponibles
            screenshot_paths_for_component = []
            if screenshot_paths:
                # Por ahora, pasamos todas las capturas a cada componente
                # En el futuro se podría filtrar por componente si fuera necesario
                screenshot_paths_for_component = screenshot_paths
            
            component_result, changes = _process_single_component_sandbox(
                template_path, client, project_root, axe_errors_for_template, screenshot_paths_for_component
            )
            processed_components.append(component_result)
            if changes:
                changes_map.append({
                    "component": component_result["component_name"],
                    "template_path": str(template_path),
                    "changes": changes
                })
            if component_result["status"] == "updated":
                stats["updated"] += 1
            relative_template = Path(component_result["template_path"]).relative_to(project_root)
            summary_lines.append(f"✓ {relative_template} -> {component_result['status']}")
        except Exception as exc:
            stats["errors"] += 1
            relative_path = template_path.relative_to(project_root)
            error_msg = f"✗ {relative_path} - Error: {exc}"
            summary_lines.append(error_msg)
            processed_components.append(
                {
                    "component_name": template_path.stem.replace(".component", ""),
                    "template_path": str(relative_path),
                    "status": "error",
                    "error": str(exc),
                }
            )

    # FASE 4: Aplicar cambios de accesibilidad al código fuente real
    print(f"\n[Fase 4] Aplicando {len(changes_map)} cambios de accesibilidad al código fuente...")
    applied_changes = _apply_changes_map(changes_map, project_root)
    
    # Verificar compilación final después de aplicar cambios de accesibilidad
    print(f"\n[Fase 5] Verificando compilación final...")
    final_build_result = _compile_and_get_errors(project_root)
    
    if not final_build_result["verification_available"]:
        print("⚠️ No se pudo verificar la compilación final (ng no disponible).")
        summary_lines.append("⚠️ Cambios aplicados pero no se pudo verificar compilación final")
    elif not final_build_result["success"]:
        stats["build_failures"] = 1
        print(f"✗ ERROR: El proyecto no compila después de aplicar los cambios ({len(final_build_result.get('errors', []))} errores).")
        print("  ⚠️ Los cambios se mantienen para que puedas corregirlos manualmente.")
        summary_lines.append(f"⚠️ Cambios aplicados pero hay {len(final_build_result.get('errors', []))} errores de compilación")
    else:
        print("✓ Proyecto compila correctamente después de todas las correcciones.")
        summary_lines.append(f"✓ Compilación verificada: {applied_changes} cambios aplicados exitosamente")
    
    # Nota: Si serve_app=True, el servidor ya se inició en la Fase 2 (antes de ejecutar Axe)
    # Solo mostramos un mensaje informativo si el servidor sigue corriendo
    if serve_app and dev_server_process:
        print(f"\n[Info] El servidor Angular está corriendo en http://localhost:4200")
        print(f"  → El servidor se mantendrá corriendo. Presiona Ctrl+C en la terminal donde se inició para detenerlo.")
    elif serve_app:
        print(f"\n[Info] Si el servidor Angular no está corriendo, puedes iniciarlo manualmente con: ng serve")

    report_payload = {
        "project_root": str(project_root),
        "stats": stats,
        "components": processed_components,
        "changes_map": changes_map,
        "build_verification": {
            "initial_build": build_result.get("success", False),
            "initial_verification_available": build_result.get("verification_available", False),
            "final_build": final_build_result.get("success", False),
            "final_verification_available": final_build_result.get("verification_available", False),
            "compilation_errors_fixed": stats["compilation_fixes"],
        },
    }

    report_path = Path(run_path) / "angular_summary.json"
    with open(report_path, "w", encoding="utf-8") as report_file:
        json.dump(report_payload, report_file, indent=2, ensure_ascii=False)

    headline = f"Componentes encontrados: {stats['templates']} | Actualizados: {stats['updated']} | Errores: {stats['errors']}"
    return [headline, "-" * len(headline), *summary_lines, f"Resumen guardado en {report_path}"]


def _load_angular_config(config_path: Path) -> Dict:
    with open(config_path, "r", encoding="utf-8") as config_file:
        return json.load(config_file)


def _get_default_project_name(project_root: Path) -> Optional[str]:
    """
    Obtiene el nombre del proyecto por defecto para workspaces multi-proyecto.
    Retorna None si es un proyecto simple o no se puede determinar.
    """
    angular_config = project_root / ANGULAR_CONFIG_FILE
    if not angular_config.exists():
        return None
    
    try:
        config = _load_angular_config(angular_config)
        projects = config.get("projects", {})
        
        # Si solo hay un proyecto, no hace falta especificarlo
        if len(projects) <= 1:
            return None
        
        # Buscar proyecto por defecto
        default_project = config.get("defaultProject")
        if default_project and default_project in projects:
            return default_project
        
        # Si no hay defaultProject, buscar el primer proyecto con architect.build
        for name, proj_config in projects.items():
            architect = proj_config.get("architect", {})
            if "build" in architect:
                return name
        
        # Fallback: primer proyecto
        return list(projects.keys())[0] if projects else None
    except Exception:
        return None


def _resolve_source_roots(project_root: Path, config: Dict) -> List[Path]:
    projects = config.get("projects", {})
    if not projects:
        return []

    source_roots: List[Path] = []

    default_project = config.get("defaultProject")
    project_names = [default_project] if default_project else []
    project_names.extend([name for name in projects.keys() if name not in project_names])

    for project_name in project_names:
        project_config = projects.get(project_name, {})
        source_root = project_config.get("sourceRoot") or project_config.get("root")
        if not source_root:
            continue
        source_path = project_root / source_root
        if source_path.exists():
            source_roots.append(source_path)

    # fallback: typical src/ directory
    fallback_src = project_root / "src"
    if not source_roots and fallback_src.exists():
        source_roots.append(fallback_src)

    return source_roots


def _discover_component_templates(source_roots: List[Path]) -> List[Path]:
    templates: List[Path] = []
    for root in source_roots:
        templates.extend(root.glob("**/*.component.html"))
    return sorted(templates)


def _process_single_component_sandbox(
    template_path: Path, client, project_root: Path, axe_errors: List[Dict] = None, screenshot_paths: List[str] = None
) -> Tuple[Dict, Optional[Dict]]:
    """
    Procesa un componente en modo sandbox, generando un mapa de cambios sin modificar el código fuente.
    
    Args:
        template_path: Ruta al template del componente
        client: Cliente OpenAI
        project_root: Ruta raíz del proyecto
        axe_errors: Lista de errores de Axe mapeados a este template (opcional)
    
    Returns:
        Tuple de (resultado del componente, mapa de cambios)
    """
    base_component_name = template_path.stem.replace(".component", "")
    component_dir = template_path.parent

    ts_path = component_dir / (template_path.name.replace(".html", ".ts"))
    styles_candidates = [
        component_dir / (template_path.name.replace(".html", ".scss")),
        component_dir / (template_path.name.replace(".html", ".sass")),
        component_dir / (template_path.name.replace(".html", ".css")),
    ]
    style_path = next((path for path in styles_candidates if path.exists()), None)

    template_content = template_path.read_text(encoding="utf-8")
    ts_content = ts_path.read_text(encoding="utf-8") if ts_path.exists() else None
    style_content = style_path.read_text(encoding="utf-8") if style_path and style_path.exists() else None

    # Analizar el template para detectar errores obvios antes de enviarlo al LLM
    detected_errors = _analyze_template_for_accessibility_errors(template_content, style_content)
    
    # Convertir errores de Axe a formato legible para el prompt
    axe_errors_formatted = []
    if axe_errors:
        import re
        print(f"  → {len(axe_errors)} errores de Axe detectados para este componente")
        for axe_error in axe_errors:
            # Extraer información de la estructura correcta de Axe
            violation = axe_error.get("violation", {})
            node = axe_error.get("node", {})
            violation_id = axe_error.get("violation_id", violation.get("id", "unknown"))
            
            # Selector CSS del nodo
            targets = node.get("target", [])
            selector = targets[0] if targets and isinstance(targets[0], str) else "No selector"
            
            # HTML del nodo afectado
            html_snippet = (node.get("html") or "").strip()
            html_display = html_snippet[:200] if html_snippet else ""  # Primeros 200 chars
            
            # Descripción de la violación
            description = violation.get("description", "")
            help_text = violation.get("help", "")
            
            # Datos específicos de contraste (si aplica)
            contrast_info = ""
            if violation_id == "color-contrast":
                # Buscar datos de contraste en los checks de Axe
                all_checks = node.get("all", []) or []
                any_checks = node.get("any", []) or []
                checks = all_checks + any_checks
                
                for check in checks:
                    check_data = check.get("data", {})
                    bg_color = check_data.get("bgColor", "")
                    fg_color = check_data.get("fgColor", "")
                    ratio = check_data.get("contrastRatio", "")
                    expected_ratio = check_data.get("expectedContrastRatio", "")
                    
                    if bg_color or fg_color or ratio:
                        contrast_info = f" | Color texto: {fg_color}, Color fondo: {bg_color}, Ratio actual: {ratio}, Ratio requerido: {expected_ratio}"
                        break
                
                # Si no encontramos datos en all/any, buscar en failureSummary o en el mensaje del check
                if not contrast_info:
                    failure_summary = node.get("failureSummary", "")
                    if failure_summary:
                        import re
                        # Extraer ratio del mensaje de error (formato: "contrast of 3.33")
                        ratio_match = re.search(r'contrast of ([\d.]+)', failure_summary, re.IGNORECASE)
                        expected_match = re.search(r'Expected contrast ratio of ([\d.]+:?[\d]*)', failure_summary, re.IGNORECASE)
                        fg_match = re.search(r'foreground color: (#[0-9a-fA-F]+)', failure_summary, re.IGNORECASE)
                        bg_match = re.search(r'background color: (#[0-9a-fA-F]+)', failure_summary, re.IGNORECASE)
                        
                        if ratio_match or expected_match:
                            ratio_str = ratio_match.group(1) if ratio_match else "N/A"
                            expected_str = expected_match.group(1) if expected_match else "4.5:1"
                            fg_str = fg_match.group(1) if fg_match else "N/A"
                            bg_str = bg_match.group(1) if bg_match else "N/A"
                            contrast_info = f" | Color texto: {fg_str}, Color fondo: {bg_str}, Ratio actual: {ratio_str}, Ratio requerido: {expected_str}"
                    
                    # Si aún no tenemos información, buscar en los mensajes de los checks
                    if not contrast_info:
                        for check in checks:
                            message = check.get("message", "")
                            if "contrast" in message.lower() and ("insufficient" in message.lower() or "ratio" in message.lower()):
                                import re
                                ratio_match = re.search(r'contrast of ([\d.]+)', message, re.IGNORECASE)
                                expected_match = re.search(r'Expected contrast ratio of ([\d.]+:?[\d]*)', message, re.IGNORECASE)
                                fg_match = re.search(r'foreground color: (#[0-9a-fA-F]+)', message, re.IGNORECASE)
                                bg_match = re.search(r'background color: (#[0-9a-fA-F]+)', message, re.IGNORECASE)
                                
                                if ratio_match:
                                    ratio_str = ratio_match.group(1)
                                    expected_str = expected_match.group(1) if expected_match else "4.5:1"
                                    fg_str = fg_match.group(1) if fg_match else "N/A"
                                    bg_str = bg_match.group(1) if bg_match else "N/A"
                                    contrast_info = f" | Color texto: {fg_str}, Color fondo: {bg_str}, Ratio actual: {ratio_str}, Ratio requerido: {expected_str}"
                                    break
            
            # Formatear error de Axe de forma muy específica y detallada
            error_parts = [f"ERROR AXE: {violation_id}"]
            
            if selector and selector != "No selector":
                error_parts.append(f"Selector CSS: {selector}")
                
                # Advertir si el selector apunta a un elemento generado por Angular Material
                if ".mdc-button__label" in selector or ".mat-button-label" in selector or " > " in selector:
                    # Extraer el selector del padre (antes de " > ")
                    parent_selector = selector.split(" > ")[0] if " > " in selector else selector.replace(".mdc-button__label", "").strip()
                    error_parts.append(f"⚠️ ATENCIÓN: Este selector apunta a un elemento interno generado por Angular Material. Busca el elemento PADRE en el template (ej: botón con {parent_selector}) y aplica el estilo allí.")
            
            if description:
                error_parts.append(f"Descripción: {description}")
            
            if contrast_info:
                error_parts.append(f"Datos contraste: {contrast_info.strip()}")
            
            if html_display:
                # Limpiar atributos Angular dinámicos para mostrar
                clean_html = re.sub(r'\s+_ngcontent-[^=]*="[^"]*"', '', html_display)
                clean_html = re.sub(r'\s+_nghost-[^=]*="[^"]*"', '', clean_html)
                error_parts.append(f"HTML afectado: {clean_html}")
                
                # Si el HTML es un span con clase mdc-button__label, advertir que es generado
                if "mdc-button__label" in clean_html or "mat-button-label" in clean_html:
                    # Intentar extraer el texto del botón para ayudar a localizarlo
                    text_match = re.search(r'>\s*([^<]+)\s*<', clean_html)
                    if text_match:
                        button_text = text_match.group(1).strip()
                        error_parts.append(f"⚠️ NOTA: Este span es generado por Angular Material. Busca el botón que contiene el texto '{button_text}' en el template.")
            
            if help_text:
                error_parts.append(f"Ayuda: {help_text}")
            
            error_msg = " | ".join(error_parts)
            
            axe_errors_formatted.append(error_msg)
            detected_errors.append(error_msg)  # Añadir también a detected_errors para que se incluyan en el prompt
    
    if detected_errors:
        print(f"  → Total de {len(detected_errors)} errores de accesibilidad detectados en {base_component_name}")
        for error in detected_errors[:5]:
            print(f"    - {error[:80]}")
    else:
        print(f"  → No se detectaron errores obvios en {base_component_name} (el LLM debe buscar más profundamente)")

    system_message = (
        "Eres un EXPERTO AUDITOR DE ACCESIBILIDAD WEB y Angular. Tu MISIÓN CRÍTICA es: "
        "1) ANALIZAR EXHAUSTIVAMENTE cada línea del código para encontrar TODOS los errores de accesibilidad (WCAG 2.2 A+AA), "
        "2) CORREGIR CADA ERROR encontrado SIN EXCEPCIÓN, incluso si requiere cambios significativos. "
        "DEBES BUSCAR ACTIVAMENTE: botones/enlaces sin texto visible ni aria-label, inputs sin labels, imágenes sin alt, "
        "problemas de contraste, falta de soporte de teclado, jerarquía de encabezados incorrecta, listas sin estructura, etc. "
        "🚨🚨🚨 CRÍTICO SOBRE CONTRASTE: Si hay errores de contraste detectados o si encuentras elementos con texto que podría tener bajo contraste, "
        "DEBES corregir TODOS los errores de contraste ajustando el color del texto y/o el fondo para que cumplan WCAG (4.5:1 para texto normal, 3:1 para texto grande). "
        "En fondos claros, normalmente se usará un color de texto oscuro (#000000, #212121, etc.); en fondos oscuros, un color de texto claro (#FFFFFF, #F5F5F5, etc.). "
        "NO corrijas solo uno, corrige TODOS. Si hay 3 errores de contraste, corrige los 3. "
        "🚨🚨🚨 CRÍTICO SOBRE DISEÑO RESPONSIVE: "
        "- PRESERVA TODOS los estilos responsive existentes (media queries, clases responsive, flexbox, grid, etc.) "
        "- NO cambies display:none a display:block a menos que sea absolutamente necesario para accesibilidad "
        "- Si un label tiene display:none, es porque está oculto visualmente pero accesible para lectores de pantalla - usa sr-only o aria-label en su lugar "
        "- NO añadas estilos inline que rompan el diseño responsive (width fijo, height fijo, margin/padding excesivos, etc.) "
        "- Mantén todas las clases de Bootstrap/CSS frameworks (col-sm-*, col-md-*, etc.) "
        "- NO modifiques propiedades de layout como display, position, flex, grid, width, height, margin, padding a menos que sea crítico para accesibilidad "
        "🚨🚨🚨 CRÍTICO SOBRE CAPTURAS DE PANTALLA (si están disponibles): "
        "Si se proporcionan capturas de pantalla en el mensaje del usuario, DEBES examinarlas detalladamente. "
        "Estas capturas muestran cómo se ve REALMENTE la aplicación en diferentes tamaños de pantalla. "
        "TU OBJETIVO: Corregir TODOS los errores de accesibilidad PERO preservar EXACTAMENTE el diseño visual que ves en las capturas. "
        "Las correcciones deben ser 'invisibles' visualmente - usa aria-label, roles, alt text, y ajustes mínimos de contraste. "
        "El resultado final debe verse IDÉNTICO a las capturas, pero accesible. "
        "IMPORTANTE: Si el código tiene CUALQUIER problema de accesibilidad, DEBES corregirlo. "
        "NO devuelvas el código original sin cambios. SIEMPRE busca y corrige errores. "
        "La accesibilidad ES IMPORTANTE Y DEBE CORREGIRSE, PERO si hay capturas, preserva el diseño visual que muestran. "
        "NO añadas comentarios HTML ni atributos que muestren que fueron correcciones. El código debe verse como si fuera original."
    )

    # Contar errores de contraste detectados
    contrast_errors = [e for e in detected_errors if 'contraste' in e.lower() or 'contrast' in e.lower()]
    if contrast_errors:
        print(f"  → {len(contrast_errors)} errores de contraste detectados - el LLM DEBE corregir TODOS")

    user_prompt = _build_component_prompt(
        component_name=base_component_name,
        template_content=template_content,
        ts_content=ts_content,
        style_content=style_content,
        template_path=str(template_path),
        ts_path=str(ts_path) if ts_path.exists() else None,
        style_path=str(style_path) if style_path else None,
        detected_errors=detected_errors,
        contrast_errors_count=len(contrast_errors),
    )

    # Preparar mensajes, incluyendo capturas de pantalla si están disponibles
    messages = [
        {"role": "system", "content": system_message},
    ]
    
    # Si hay capturas de pantalla, incluirlas en el mensaje del usuario
    if screenshot_paths:
        import base64
        screenshot_instructions = """
📸 CAPTURAS DE PANTALLA - CRÍTICO PARA PRESERVAR EL DISEÑO:

He tomado capturas de la aplicación en diferentes tamaños de pantalla (mobile, tablet, desktop) que muestran cómo se ve REALMENTE la página antes de las correcciones.

🚨 INSTRUCCIONES OBLIGATORIAS SOBRE LAS CAPTURAS:
1. EXAMINA DETALLADAMENTE cada captura para entender:
   - El diseño visual actual (layout, colores, espaciado, distribución)
   - Cómo se adapta el contenido en diferentes tamaños de pantalla
   - Qué elementos son visibles/ocultos en cada tamaño
   - El estilo visual general de la aplicación

2. CORRIGE TODOS LOS ERRORES de accesibilidad listados arriba, PERO:
   - MANTÉN el diseño visual que ves en las capturas
   - NO cambies colores de fondo, tamaños de elementos, o distribución que se vea en las imágenes
   - Para errores de contraste: ajusta SOLO el color del texto, manteniendo el fondo visible en las capturas
   - NO añadas elementos visibles nuevos (usa aria-label o sr-only en su lugar)
   - NO cambies display:none a display:block si en las capturas no se ve ese elemento
   - Respeta el diseño responsive: si en mobile se ve de una forma, mantén esa forma

3. TU OBJETIVO: Corregir TODOS los errores de accesibilidad SIN cambiar cómo se ve la página en las capturas.
   - Las correcciones deben ser "invisibles" visualmente
   - Usa aria-label, roles, alt text, y ajustes de contraste mínimos
   - El diseño final debe verse IDÉNTICO a las capturas, pero accesible

Las capturas muestran la aplicación ANTES de las correcciones. Tu trabajo es hacerla accesible manteniendo exactamente ese aspecto visual.
"""
        user_content = [
            {"type": "text", "text": user_prompt + screenshot_instructions}
        ]
        # Añadir cada captura como imagen
        for screenshot_path in screenshot_paths:
            try:
                screenshot_file = Path(screenshot_path)
                if screenshot_file.exists():
                    # Leer y codificar la imagen en base64
                    with open(screenshot_file, "rb") as img_file:
                        image_base64 = base64.b64encode(img_file.read()).decode('utf-8')
                        # Determinar el tipo MIME basado en la extensión
                        mime_type = "image/png"  # Por defecto PNG
                        if screenshot_path.endswith('.jpg') or screenshot_path.endswith('.jpeg'):
                            mime_type = "image/jpeg"
                        user_content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_base64}"
                            }
                        })
            except Exception as e:
                print(f"  ⚠️ Error al incluir captura {screenshot_path}: {e}")
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": user_prompt})

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        temperature=0.0,
    )

    response_text = response.choices[0].message.content or ""
    log_openai_call(prompt=user_prompt, response=response_text, model="gpt-4o", call_type="angular_component_fix")

    print(f"  → LLM respondió con {len(response_text)} caracteres")
    
    # Debug: mostrar primeros caracteres de la respuesta para ver qué está devolviendo
    print(f"  → Primeros 200 caracteres de respuesta: {response_text[:200]}")
    
    try:
        parsed_response = _parse_component_response(response_text)
        print(f"  → Respuesta parseada correctamente")
    except Exception as e:
        print(f"  ✗ Error parseando respuesta del LLM: {e}")
        print(f"  → Primeros 500 caracteres de la respuesta: {response_text[:500]}")
        # Intentar extraer template directamente si el parsing falla
        import re
        template_match = re.search(r'<<<TEMPLATE>>>\s*(.*?)\s*<<<END TEMPLATE>>>', response_text, re.DOTALL)
        if template_match:
            parsed_response = {"template": template_match.group(1).strip(), "typescript": None, "styles": None}
            print(f"  → Template extraído usando regex alternativo")
        else:
            print(f"  ✗ No se pudo extraer template de ninguna forma")
            return {
                "component_name": base_component_name,
                "template_path": str(template_path),
                "typescript_path": str(ts_path) if ts_path.exists() else None,
                "styles_path": str(style_path) if style_path else None,
                "status": "error",
                "error": f"Error parseando respuesta: {e}",
                "changes": {}
            }, None
    
    # Corregir sintaxis Angular para atributos ARIA con binding
    template_content_corrected = _fix_angular_aria_syntax(parsed_response.get("template"))
    
    # Corregir errores de sintaxis básicos comunes (comillas mal cerradas, tags no cerrados, etc.)
    template_content_corrected = _fix_basic_syntax_errors(template_content_corrected)
    
    # Aplicar correcciones automáticas de accesibilidad (role="img" en iconos, lang en html, etc.)
    template_content_corrected = _apply_automatic_accessibility_fixes(template_content_corrected)
    
    # Validar y corregir cambios que rompan el responsive
    template_content_corrected = _fix_responsive_breaking_changes(template_content, template_content_corrected)
    
    if not template_content_corrected:
        print(f"  ⚠️ No se obtuvo template corregido del LLM")
        return {
            "component_name": base_component_name,
            "template_path": str(template_path),
            "typescript_path": str(ts_path) if ts_path.exists() else None,
            "styles_path": str(style_path) if style_path else None,
            "status": "error",
            "error": "No se pudo obtener template corregido",
            "changes": {}
        }, None
    
    # Aplicar correcciones automáticas para errores de contraste detectados
    # IMPORTANTE: estas correcciones automáticas se han desactivado por defecto
    # porque pueden elegir un color incorrecto cuando el fondo real es oscuro.
    # Preferimos que el LLM (con el contexto completo) y/o el desarrollador
    # ajusten el contraste de forma explícita.
    contrast_errors = [e for e in detected_errors if 'contraste' in e.lower() or 'contrast' in e.lower()]
    if contrast_errors and ENABLE_AUTOMATIC_CONTRAST_FIXES:
        print(f"  → Aplicando correcciones automáticas para {len(contrast_errors)} errores de contraste detectados")
        template_content_corrected = _apply_automatic_contrast_fixes(template_content_corrected, contrast_errors)
    
    print(f"  → Template corregido: {len(template_content_corrected)} caracteres (original: {len(template_content)} caracteres)")
    
    # Comparación más robusta - normalizar espacios pero mantener estructura
    original_clean = '\n'.join(line.rstrip() for line in template_content.split('\n'))
    corrected_clean = '\n'.join(line.rstrip() for line in template_content_corrected.split('\n'))
    
    # Generar mapa de cambios sin aplicar todavía (sandbox)
    changes = {}
    
    # Comparar de múltiples formas
    are_different = (
        original_clean.strip() != corrected_clean.strip() or
        len(original_clean.strip()) != len(corrected_clean.strip()) or
        template_content.strip() != template_content_corrected.strip()
    )
    
    # Si hay errores detectados automáticamente, forzar que se consideren cambios
    # incluso si la comparación no los detecta (el LLM puede haber hecho cambios sutiles)
    if detected_errors and not are_different:
        print(f"  ⚠️ No se detectaron diferencias en la comparación, pero hay {len(detected_errors)} errores detectados automáticamente")
        print(f"  → Forzando aplicación de cambios porque hay errores que deben corregirse")
        are_different = True
    
    # Debug: mostrar diferencias específicas si no se detectan
    if not are_different:
        print(f"  ⚠️ El template corregido parece IDÉNTICO al original")
        print(f"  → Comparando líneas...")
        original_lines = template_content.strip().split('\n')
        corrected_lines = template_content_corrected.strip().split('\n')
        if len(original_lines) != len(corrected_lines):
            print(f"    → Diferente número de líneas: {len(original_lines)} vs {len(corrected_lines)}")
            are_different = True
        else:
            print(f"    → Mismo número de líneas: {len(original_lines)}")
            # Buscar diferencias línea por línea
            differences_found = False
            for i, (orig, corr) in enumerate(zip(original_lines, corrected_lines)):
                if orig.strip() != corr.strip():
                    print(f"    → Diferencia en línea {i+1}:")
                    print(f"      Original: {orig[:100]}")
                    print(f"      Corregido: {corr[:100]}")
                    differences_found = True
                    are_different = True
                    break
            if not differences_found:
                print(f"    → No se encontraron diferencias línea por línea")
                # Si hay errores detectados, forzar cambios de todas formas
                if detected_errors:
                    print(f"    → PERO hay {len(detected_errors)} errores detectados, forzando aplicación de cambios")
                    are_different = True
    
    if are_different:
        print(f"  ✓ Cambios detectados en template de {base_component_name}")
        print(f"    → Original: {len(original_clean.strip())} chars, Corregido: {len(corrected_clean.strip())} chars")
        changes["template"] = {
            "path": str(template_path),
            "original": template_content,
            "corrected": template_content_corrected
        }
    else:
        print(f"  ⚠️ No se detectaron cambios en template de {base_component_name}")
        print(f"    → El LLM devolvió el mismo código. Esto indica que:")
        print(f"      1. El LLM no detectó errores de accesibilidad")
        print(f"      2. El LLM detectó errores pero no los corrigió")
        print(f"      3. El template realmente no tiene errores (poco probable)")
        
        # Mostrar errores detectados automáticamente si los hay
        if detected_errors:
            print(f"    → Se detectaron {len(detected_errors)} errores automáticamente, pero el LLM no los corrigió")
            for error in detected_errors[:5]:
                print(f"      - {error[:80]}")
            # Forzar cambios si hay errores detectados
            print(f"    → FORZANDO aplicación de cambios porque hay errores detectados")
            changes["template"] = {
                "path": str(template_path),
                "original": template_content,
                "corrected": template_content_corrected
            }
    
    if ts_content is not None:
        ts_corrected = parsed_response.get("typescript")
        if ts_corrected and ts_corrected.strip() != ts_content.strip():
            changes["typescript"] = {
                "path": str(ts_path),
                "original": ts_content,
                "corrected": ts_corrected
            }
    
    if style_path and style_content is not None:
        style_corrected = parsed_response.get("styles")
        if style_corrected and style_corrected.strip() != style_content.strip():
            changes["styles"] = {
                "path": str(style_path),
                "original": style_content,
                "corrected": style_corrected
            }

    status = "updated" if changes else "unchanged"

    result = {
        "component_name": base_component_name,
        "template_path": str(template_path),
        "typescript_path": str(ts_path) if ts_path.exists() else None,
        "styles_path": str(style_path) if style_path else None,
        "status": status,
        "changes": {
            "template": "template" in changes,
            "typescript": "typescript" in changes,
            "styles": "styles" in changes,
        },
    }
    
    return result, changes if changes else None


def _categorize_errors(detected_errors: List[str]) -> Dict[str, List[str]]:
    """Agrupa los errores detectados por tipo"""
    categories = {
        "missing_alt": [],
        "missing_label": [],
        "missing_aria_label": [],
        "contrast": [],
        "other": []
    }
    
    for error in detected_errors:
        error_lower = error.lower()
        if "imagen sin alt" in error_lower or "sin alt" in error_lower:
            categories["missing_alt"].append(error)
        elif "sin label" in error_lower or "input sin" in error_lower:
            categories["missing_label"].append(error)
        elif "botón sin" in error_lower or "enlace sin" in error_lower or "aria-label" in error_lower:
            categories["missing_aria_label"].append(error)
        elif "contraste" in error_lower or "contrast" in error_lower:
            categories["contrast"].append(error)
        else:
            categories["other"].append(error)
    
    return categories


def _build_error_specific_prompt(error_type: str, errors: List[str]) -> str:
    """Construye un prompt específico y conciso para un tipo de error"""
    if not errors:
        return ""
    
    if error_type == "missing_alt":
        return f"""🔴 ERRORES DE IMÁGENES SIN ALT ({len(errors)} encontrados):
{chr(10).join(f"- {e}" for e in errors)}

ACCIÓN REQUERIDA: Añade el atributo alt a TODAS las imágenes mencionadas.
- Si la imagen es informativa: alt="Descripción de la imagen"
- Si la imagen es decorativa: alt=""
- En Angular, usa [alt] para binding dinámico o alt="texto fijo" para estático

CORRIGE TODAS las imágenes listadas arriba."""
    
    elif error_type == "missing_label":
        return f"""🔴 ERRORES DE INPUTS SIN LABEL ({len(errors)} encontrados):
{chr(10).join(f"- {e}" for e in errors)}

ACCIÓN REQUERIDA: Añade <label> asociado a TODOS los inputs mencionados.
IMPORTANTE SOBRE RESPONSIVE:
- Si el input ya tiene un label con display:none, NO lo cambies a display:block
- En su lugar, añade aria-label al input: <input id="inputId" aria-label="Descripción" ... />
- O usa una clase sr-only (screen-reader-only) para el label: <label for="inputId" class="sr-only">Texto</label>
- Solo cambia display si el label NO existe y es necesario que sea visible

Ejemplo correcto (preservando responsive):
  <label for="inputId" class="sr-only">Texto del label</label>
  <input id="inputId" ... />
  
O alternativamente:
  <input id="inputId" aria-label="Texto del label" ... />

CORRIGE TODOS los inputs listados arriba."""
    
    elif error_type == "missing_aria_label":
        return f"""🔴 ERRORES DE BOTONES/ENLACES SIN ARIA-LABEL ({len(errors)} encontrados):
{chr(10).join(f"- {e}" for e in errors)}

ACCIÓN REQUERIDA: Añade aria-label descriptivo a TODOS los botones/enlaces mencionados.
- Para valores estáticos: aria-label="Descripción"
- Para binding dinámico en Angular: [attr.aria-label]="variable"

CORRIGE TODOS los elementos listados arriba."""
    
    elif error_type == "contrast":
        return f"""🔴 ERRORES DE CONTRASTE ({len(errors)} encontrados):
{chr(10).join(f"- {e}" for e in errors)}

ACCIÓN REQUERIDA: Corrige el contraste de color de TODOS los elementos mencionados.
- Ratio mínimo requerido: 4.5:1 para texto normal, 3:1 para texto grande
- En fondos claros: usa style="color: #000000" o #212121
- En fondos oscuros: usa style="color: #FFFFFF" o #F5F5F5
- Busca TODOS los elementos similares y corrígelos también

CORRIGE TODOS los elementos con bajo contraste listados arriba."""
    
    else:
        return f"""🔴 OTROS ERRORES ({len(errors)} encontrados):
{chr(10).join(f"- {e}" for e in errors)}

ACCIÓN REQUERIDA: Corrige estos errores de accesibilidad."""
    
    return ""


def _format_detected_errors(detected_errors: List[str]) -> str:
    """Formatea los errores detectados con prompts específicos por tipo"""
    if not detected_errors:
        return ""
    
    # Separar errores de Axe de errores estáticos
    axe_errors = [e for e in detected_errors if e.startswith("ERROR AXE:")]
    static_errors = [e for e in detected_errors if not e.startswith("ERROR AXE:")]
    
    categories = _categorize_errors(static_errors)
    
    prompts = []
    
    # Añadir errores de Axe primero (son más específicos)
    if axe_errors:
        error_list = "\n".join([f"\n{i+1}. {e}" for i, e in enumerate(axe_errors)])
        prompts.append(f"""🔴 ERRORES DE AXE DETECTADOS ({len(axe_errors)} encontrados):
Estos son errores REALES detectados por la herramienta de accesibilidad Axe en la aplicación renderizada. DEBES corregirlos TODOS sin excepción.

{error_list}

ACCIÓN REQUERIDA PARA CADA ERROR:
1. Localiza el elemento en el template usando:
   - El selector CSS proporcionado (ej: "button[type=\"submit\"] > .mdc-button__label")
     * Los selectores de Axe pueden tener clases CSS específicas - búscalas en el template
     * Si el selector tiene ">" (hijo directo), busca la estructura padre > hijo en el template
     * Si el selector tiene clases como ".mdc-button__label", busca elementos con class="..." que contengan esa clase
   - O el fragmento HTML mostrado (puede tener atributos Angular dinámicos que debes ignorar)
     * Ignora atributos Angular dinámicos como _ngcontent-* y _nghost-*
     * Busca por el contenido del texto, los atributos estáticos, y la estructura
   - IMPORTANTE: Si no encuentras el selector exacto, busca variaciones:
     * Busca por el texto contenido (ej: "Login", "Save", etc.)
     * Busca por clases CSS similares
     * Busca por estructura HTML similar

2. Corrige el error específico:
   - Si es "color-contrast": 
     * CRÍTICO: Estos son errores REALES detectados en la aplicación renderizada. DEBES corregirlos TODOS.
     * Los datos de contraste muestran el color REAL en el HTML renderizado (después de aplicar CSS)
     * Si el template ya tiene un style="color: ..." pero Axe detecta un color diferente, significa que el CSS lo está sobrescribiendo
     * SOLUCIÓN OBLIGATORIA: Añade !important al estilo inline para que sobrescriba el CSS: style="color: #000000 !important;"
     * Reglas de corrección:
       - Si ratio actual < 4.5 (texto normal) o < 3.0 (texto grande), el contraste es INSUFICIENTE y DEBE corregirse
       - En fondos CLAROS (blanco, gris claro, etc.): usa texto OSCURO (color="#000000" o color="#212121")
       - En fondos OSCUROS (negro, gris oscuro, colores oscuros): usa texto CLARO (color="#FFFFFF" o color="#F5F5F5")
       - Ejemplo: Si Axe detecta ratio 3.33 (insuficiente), y el fondo es #ff4081 (rosa), y el texto es #ffffff (blanco),
         cambia el texto a color oscuro: style="color: #000000 !important;" o cambia el fondo a uno más claro
       - SIEMPRE añade !important para asegurar que el estilo se aplique sobre el CSS existente
     * LOCALIZACIÓN: Busca el elemento usando el selector CSS proporcionado (ej: "button[type=\"submit\"] > .mdc-button__label")
       o busca el fragmento HTML mostrado en el template
       * ⚠️ CRÍTICO - Elementos generados por Angular Material:
         Si el selector apunta a ".mdc-button__label", ".mat-button-label", o cualquier elemento con " > " que apunte a un span/div interno,
         ese elemento NO existe en tu template - Angular Material lo genera automáticamente en el DOM renderizado.
         
         EJEMPLO ESPECÍFICO:
         - Error de Axe: Selector ".mat-warn > .mdc-button__label", HTML "<span class="mdc-button__label">Get Started</span>"
         - En tu template encontrarás: <button mat-button color="warn">Get Started</button>
         - SOLUCIÓN: Añade el estilo AL BOTÓN padre:
           <button mat-button color="warn" style="color: #000000 !important;">Get Started</button>
         - El estilo con !important se aplicará al texto dentro del botón, incluyendo el span interno generado por Angular Material
         
         REGLA GENERAL:
         - Si el selector tiene " > .mdc-button__label" o " > .mat-button-label", busca el botón padre en el template
         - Extrae el selector del padre (la parte antes de " > ")
         - Busca ese botón en el template (puede tener color="warn", class="mat-warn", o el texto del botón)
         - Aplica style="color: [color-correcto] !important;" directamente al botón
         - Si el ratio es insuficiente y el fondo es claro (#fafafa, blanco, etc.), usa color oscuro (#000000)
         - Si el ratio es insuficiente y el fondo es oscuro, usa color claro (#FFFFFF)
   - Si es "link-name" o "button-name": Añade aria-label descriptivo al enlace/botón
   - Si es otro error: Sigue la descripción y ayuda proporcionadas

3. Para errores de contraste: 
   - Los datos muestran el color REAL detectado por Axe en el HTML renderizado
   - Si el template tiene un color diferente, significa que el CSS lo está sobrescribiendo
   - DEBES usar !important en el estilo inline para asegurar que se aplique: style="color: #000000 !important;"
   - NO devuelvas el código sin corregir estos errores - son errores REALES que existen en la aplicación

⚠️ CRÍTICO: Estos errores EXISTEN en la aplicación renderizada. NO devuelvas el mismo código. DEBES hacer cambios visibles.""")
    
    # Añadir errores estáticos categorizados
    for error_type, errors in categories.items():
        if errors:
            prompts.append(_build_error_specific_prompt(error_type, errors))
    
    if not prompts:
        return ""
    
    return f"""

🚨 ERRORES DE ACCESIBILIDAD DETECTADOS - CORRIGE TODOS:

{chr(10).join(prompts)}

⚠️ CRÍTICO: DEBES corregir TODOS estos errores. NO devuelvas el código original sin cambios.
"""


def _analyze_template_for_accessibility_errors(template_content: str, style_content: Optional[str] = None) -> List[str]:
    """Analiza el template y CSS para detectar errores obvios de accesibilidad usando análisis de texto crudo"""
    errors = []
    import re
    
    try:
        # Análisis basado en texto crudo para manejar mejor Angular
        lines = template_content.split('\n')
        
        # Buscar botones sin texto ni aria-label (buscar en HTML crudo)
        button_pattern = r'<button[^>]*>'
        for i, line in enumerate(lines, 1):
            if re.search(button_pattern, line, re.IGNORECASE):
                # Verificar si tiene aria-label (estático o con binding)
                has_aria_label = (
                    'aria-label=' in line or 
                    '[attr.aria-label]' in line or
                    'aria-labelledby=' in line
                )
                # Extraer el contenido del botón (texto entre > y <)
                button_match = re.search(r'<button[^>]*>(.*?)</button>', line, re.DOTALL | re.IGNORECASE)
                if button_match:
                    button_content = button_match.group(1)
                    # Limpiar contenido Angular y HTML
                    button_text = re.sub(r'\{[^}]*\}|<[^>]+>|\*ng[A-Za-z]*="[^"]*"', '', button_content).strip()
                    # Si no tiene texto visible ni aria-label, es un error
                    if not button_text and not has_aria_label:
                        errors.append(f"Línea {i}: Botón sin texto visible ni aria-label")
                elif not has_aria_label:
                    # Botón que puede estar en múltiples líneas
                    errors.append(f"Línea {i}: Botón posiblemente sin aria-label (verificar manualmente)")
        
        # Buscar enlaces sin texto descriptivo
        link_pattern = r'<a[^>]*>'
        for i, line in enumerate(lines, 1):
            if re.search(link_pattern, line, re.IGNORECASE):
                has_aria_label = (
                    'aria-label=' in line or 
                    '[attr.aria-label]' in line
                )
                link_match = re.search(r'<a[^>]*>(.*?)</a>', line, re.DOTALL | re.IGNORECASE)
                if link_match:
                    link_text = re.sub(r'\{[^}]*\}|<[^>]+>', '', link_match.group(1)).strip()
                    if not link_text and not has_aria_label:
                        errors.append(f"Línea {i}: Enlace sin texto ni aria-label")
                    elif link_text.lower().strip() in ['click aquí', 'más', 'aquí', 'click here', 'más info', 'ver más']:
                        errors.append(f"Línea {i}: Enlace con texto genérico '{link_text}' necesita aria-label descriptivo")
        
        # Buscar inputs sin label (buscar por id y for)
        input_pattern = r'<(input|select|textarea)[^>]*>'
        input_ids = []
        label_fors = []
        
        for i, line in enumerate(lines, 1):
            # Buscar inputs y sus IDs
            input_match = re.search(input_pattern, line, re.IGNORECASE)
            if input_match:
                id_match = re.search(r'\bid=["\']([^"\']+)["\']', line)
                if id_match:
                    input_ids.append(id_match.group(1))
                else:
                    # Input sin ID - verificar si tiene aria-label
                    has_aria_label = (
                        'aria-label=' in line or 
                        '[attr.aria-label]' in line or
                        'aria-labelledby=' in line
                    )
                    if not has_aria_label:
                        errors.append(f"Línea {i}: Input sin id ni aria-label (necesita label asociado)")
            
            # Buscar labels y sus atributos for
            label_match = re.search(r'<label[^>]*>', line, re.IGNORECASE)
            if label_match:
                for_match = re.search(r'\bfor=["\']([^"\']+)["\']', line)
                if for_match:
                    label_fors.append(for_match.group(1))
        
        # Verificar inputs sin label asociado
        for inp_id in input_ids:
            if inp_id not in label_fors:
                # Verificar si el input tiene aria-label en alguna línea cercana
                found_aria = False
                for line in lines:
                    if inp_id in line and ('aria-label=' in line or '[attr.aria-label]' in line):
                        found_aria = True
                        break
                if not found_aria:
                    errors.append(f"Input con id='{inp_id}' sin label asociado (usar <label for=\"{inp_id}\">)")
        
        # Buscar imágenes sin alt
        img_pattern = r'<img[^>]*>'
        for i, line in enumerate(lines, 1):
            if re.search(img_pattern, line, re.IGNORECASE):
                if 'alt=' not in line:
                    errors.append(f"Línea {i}: Imagen sin atributo alt")
        
        # Buscar elementos con texto que podrían tener problemas de contraste
        # Buscar <p>, <a>, <span>, <div>, <h1-h6> sin color explícito
        text_elements_pattern = r'<(p|a|span|div|h[1-6]|label|button)[^>]*>'
        for i, line in enumerate(lines, 1):
            if re.search(text_elements_pattern, line, re.IGNORECASE):
                # Verificar si tiene texto visible
                element_match = re.search(r'<(p|a|span|div|h[1-6]|label|button)[^>]*>(.*?)</\1>', line, re.DOTALL | re.IGNORECASE)
                if element_match:
                    element_text = re.sub(r'\{[^}]*\}|<[^>]+>', '', element_match.group(2)).strip()
                    if element_text and len(element_text) > 10:  # Solo si tiene texto significativo
                        # Verificar si tiene color explícito
                        has_explicit_color = (
                            'style=' in line and ('color:' in line or 'color=' in line) or
                            '[style.color]' in line or
                            '[ngStyle]' in line
                        )
                        # Verificar si tiene clases que podrían causar problemas
                        has_problematic_class = any(cls in line for cls in ['text-muted', 'text-secondary', 'text-light', 'text-gray', 'btn'])
                        if not has_explicit_color and (has_problematic_class or 'class=' in line):
                            errors.append(f"Línea {i}: Posible error de contraste - {element_match.group(1)} con texto sin color explícito (añadir style='color: #000000')")
        
        # Analizar CSS para detectar posibles problemas de contraste
        if style_content:
            contrast_errors = _analyze_css_for_contrast_issues(style_content, lines)
            errors.extend(contrast_errors)
        
    except Exception as e:
        print(f"  ⚠️ Error analizando template: {e}")
        import traceback
        traceback.print_exc()
    
    return errors


def _analyze_css_for_contrast_issues(style_content: str, template_lines: List[str]) -> List[str]:
    """Analiza el CSS para detectar posibles problemas de contraste"""
    errors = []
    import re
    
    try:
        # Buscar clases comunes que suelen tener problemas de contraste
        problematic_classes = ['text-muted', 'text-secondary', 'text-light', 'text-white', 'text-gray-300', 'text-gray-400', 'text-gray-500']
        for j, template_line in enumerate(template_lines, 1):
            for problematic_class in problematic_classes:
                if problematic_class in template_line:
                    errors.append(f"Línea {j}: Posible error de contraste - clase '{problematic_class}' detectada (añadir style='color: #000000')")
        
        # Buscar colores claros en el CSS
        css_lines = style_content.split('\n')
        for i, css_line in enumerate(css_lines, 1):
            # Buscar reglas de color que puedan tener bajo contraste
            if re.search(r'color\s*:', css_line, re.IGNORECASE):
                # Verificar si es un color claro (heurística simple)
                color_match = re.search(r'color\s*:\s*(#[a-f0-9]{3,6}|rgba?\([^)]+\))', css_line, re.IGNORECASE)
                if color_match:
                    color_value = color_match.group(1).lower()
                    # Detectar colores claros
                    if (color_value.startswith('#f') or color_value.startswith('#e') or 
                        color_value.startswith('#d') or 
                        ('rgba' in color_value and any(x in color_value for x in ['0.8', '0.7', '0.6', '0.5']))):
                        # Buscar el selector asociado
                        selector_match = re.search(r'^[^{]+', css_line)
                        if selector_match:
                            selector = selector_match.group(0).strip()
                            # Buscar si este selector se usa en el template
                            for j, template_line in enumerate(template_lines, 1):
                                if selector.replace('.', '').replace('#', '') in template_line:
                                    errors.append(f"Línea {j}: Posible error de contraste - color claro '{color_value}' detectado en CSS")
                                    break
        
    except Exception as e:
        # No fallar si hay error analizando CSS
        pass
    
    return errors


def _build_component_prompt(
    component_name: str,
    template_content: str,
    ts_content: Optional[str],
    style_content: Optional[str],
    template_path: str,
    ts_path: Optional[str],
    style_path: Optional[str],
    detected_errors: List[str] = None,
    contrast_errors_count: int = 0,
) -> str:
    ts_section = f"\n---\nTypeScript ({ts_path}):\n```ts\n{ts_content}\n```" if ts_content is not None else "\n---\nTypeScript: (no proporcionado)"
    style_section = (
        f"\n---\nEstilos ({style_path}):\n```css\n{style_content}\n```"
        if style_content is not None
        else "\n---\nEstilos: (no proporcionados)"
    )

    # Construir sección de errores específicos
    errors_section = _format_detected_errors(detected_errors if detected_errors else [])
    
    # Si no hay errores detectados, hacer un prompt más corto
    if not detected_errors:
        return f"""Componente Angular: {component_name}
Template: {template_path}

TAREA: Revisa y corrige TODOS los errores de accesibilidad (WCAG 2.2 A+AA) que encuentres.

Busca específicamente:
- Botones/enlaces sin texto visible ni aria-label
- Inputs sin <label> asociado
- Imágenes sin atributo alt
- Elementos con bajo contraste de color (ratio mínimo 4.5:1)
- Elementos interactivos sin soporte de teclado

IMPORTANTE: Si encuentras errores, CORRÍGELOS. NO devuelvas el código sin cambios.

Template actual:
```html
{template_content}
```
{ts_section}
{style_section}

Formato de respuesta:
<<<TEMPLATE>>>
...template HTML corregido...
<<<END TEMPLATE>>>
<<<TYPESCRIPT>>>
...TypeScript actualizado o original...
<<<END TYPESCRIPT>>>
<<<STYLES>>>
...Estilos actualizados o original...
<<<END STYLES>>>
""".strip()
    
    # Si hay errores detectados, usar prompt más enfocado
    return f"""Componente Angular: {component_name}
Template: {template_path}

TAREA: Corrige TODOS los errores de accesibilidad listados abajo.

{errors_section}

REGLAS GENERALES:
- Mantén toda la lógica Angular (bindings, *ngIf, *ngFor, pipes, etc.)
- Para atributos ARIA con binding dinámico: usa [attr.aria-*] en lugar de aria-*
- Para valores estáticos: usa aria-label="texto fijo"
- NO añadas comentarios HTML ni metadatos sobre correcciones

🚨 PRESERVA EL DISEÑO RESPONSIVE Y VISUAL (CRÍTICO):
Si se proporcionaron CAPTURAS DE PANTALLA arriba, SON TU REFERENCIA VISUAL. El diseño final debe verse IDÉNTICO a las capturas.

- NO cambies display:none a display:block - si un label está oculto visualmente, usa aria-label en el input o una clase sr-only para el label
- NO añadas estilos inline que rompan el responsive (width fijo, margin/padding excesivos, etc.)
- Mantén todas las clases responsive existentes (col-sm-*, col-md-*, etc.)
- NO modifiques propiedades de layout (display, position, flex, grid, width, height, margin, padding) a menos que sea crítico para accesibilidad
- Si un elemento tiene display:none por diseño responsive, NO lo cambies - usa aria-label en su lugar para accesibilidad
- Para errores de contraste: SOLO ajusta el color del texto (usa !important si es necesario), NO cambies el fondo ni el layout
- CORRIGE TODOS los errores de accesibilidad, pero hazlo de forma "invisible" - el resultado visual debe ser idéntico a las capturas

Template actual:
```html
{template_content}
```
{ts_section}
{style_section}

Formato de respuesta:
<<<TEMPLATE>>>
...template HTML corregido...
<<<END TEMPLATE>>>
<<<TYPESCRIPT>>>
...TypeScript actualizado o original...
<<<END TYPESCRIPT>>>
<<<STYLES>>>
...Estilos actualizados o original...
<<<END STYLES>>>
""".strip()


def _parse_component_response(response_text: str) -> Dict[str, Optional[str]]:
    sections = {
        "template": _extract_between_markers(response_text, "<<<TEMPLATE>>>", "<<<END TEMPLATE>>>"),
        "typescript": _extract_between_markers(response_text, "<<<TYPESCRIPT>>>", "<<<END TYPESCRIPT>>>"),
        "styles": _extract_between_markers(response_text, "<<<STYLES>>>", "<<<END STYLES>>>"),
    }

    for key, value in sections.items():
        if value is not None:
            # Limpiar markdown del código (```ts, ```typescript, ```css, ```scss, etc.)
            value = _clean_code_from_markdown(value)
            sections[key] = value.strip()

    if sections["template"] is None:
        raise ValueError("La respuesta del modelo no contiene la sección <<<TEMPLATE>>> requerida.")

    return sections


def _clean_code_from_markdown(code: str) -> str:
    """
    Limpia el código de cualquier markdown que pueda haber incluido el LLM.
    Elimina bloques de código markdown (```ts, ```typescript, ```css, etc.)
    """
    import re
    
    # Eliminar bloques de código markdown al inicio
    # Patrón: ```ts, ```typescript, ```css, ```scss, ```html, etc.
    code = re.sub(r'^```[a-z]*\s*\n?', '', code, flags=re.MULTILINE)
    
    # Eliminar cierre de bloques markdown al final
    code = re.sub(r'\n?```\s*$', '', code, flags=re.MULTILINE)
    
    # Eliminar cualquier ``` que quede en el código
    code = re.sub(r'```[a-z]*', '', code)
    code = re.sub(r'```', '', code)
    
    return code.strip()


def _extract_between_markers(text: str, start_marker: str, end_marker: str) -> Optional[str]:
    start_idx = text.find(start_marker)
    end_idx = text.find(end_marker)
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return None
    return text[start_idx + len(start_marker) : end_idx].strip()


def _apply_automatic_contrast_fixes(template_content: str, contrast_errors: List[str]) -> str:
    """Aplica correcciones automáticas de contraste a los elementos detectados"""
    import re
    
    lines = template_content.split('\n')
    corrected_lines = []
    
    for i, line in enumerate(lines, 1):
        corrected_line = line
        
        # Buscar errores de contraste que mencionen esta línea
        for error in contrast_errors:
            if f"Línea {i}:" in error:
                # Extraer el tipo de elemento del error
                element_match = re.search(r'Línea \d+: Posible error de contraste - (\w+)', error)
                if element_match:
                    element_type = element_match.group(1)
                    
                    # Buscar el elemento en la línea
                    element_pattern = rf'<{element_type}[^>]*>'
                    element_match_in_line = re.search(element_pattern, line, re.IGNORECASE)
                    
                    if element_match_in_line:
                        element_tag = element_match_in_line.group(0)
                        
                        # Verificar si ya tiene style
                        if 'style=' not in element_tag:
                            # Añadir style="color: #000000"
                            corrected_tag = element_tag.rstrip('>') + ' style="color: #000000">'
                            corrected_line = line.replace(element_tag, corrected_tag)
                            print(f"    → Línea {i}: Añadido style='color: #000000' a <{element_type}>")
                        elif 'color:' not in element_tag and 'color=' not in element_tag:
                            # Tiene style pero no color, añadir color
                            if 'style="' in element_tag:
                                corrected_tag = element_tag.replace('style="', 'style="color: #000000; ')
                            elif "style='" in element_tag:
                                corrected_tag = element_tag.replace("style='", "style='color: #000000; ")
                            else:
                                # style sin comillas (raro pero posible)
                                corrected_tag = element_tag.rstrip('>') + ' style="color: #000000">'
                            corrected_line = line.replace(element_tag, corrected_tag)
                            print(f"    → Línea {i}: Añadido color: #000000 al style existente de <{element_type}>")
        
        corrected_lines.append(corrected_line)
    
    return '\n'.join(corrected_lines)


def _fix_responsive_breaking_changes(original: str, corrected: str) -> str:
    """
    Detecta y corrige cambios que rompen el diseño responsive.
    Específicamente revierte cambios de display:none a display:block en labels.
    """
    if not original or not corrected:
        return corrected
    
    import re
    
    # Buscar en el original labels con display:none (en style o como atributo)
    original_display_none_labels = re.findall(
        r'<label[^>]*(?:style="[^"]*display\s*:\s*none[^"]*"|class="[^"]*visually-hidden[^"]*")[^>]*>.*?</label>',
        original,
        re.DOTALL | re.IGNORECASE
    )
    
    # También buscar labels con hidden attribute
    original_hidden_labels = re.findall(
        r'<label[^>]*hidden[^>]*>.*?</label>',
        original,
        re.DOTALL | re.IGNORECASE
    )
    
    all_original_labels = original_display_none_labels + original_hidden_labels
    
    if not all_original_labels:
        return corrected
    
    # Para cada label oculto en el original, verificar si se cambió en el corregido
    for original_label in all_original_labels:
        # Extraer el contenido del label (texto entre > y <)
        label_match = re.search(r'<label[^>]*>(.*?)</label>', original_label, re.DOTALL)
        if not label_match:
            continue
        
        label_content = label_match.group(1).strip()
        # Buscar el for attribute
        for_attr_match = re.search(r'for="([^"]+)"', original_label)
        if not for_attr_match:
            continue
        
        for_value = for_attr_match.group(1)
        
        # Buscar en el corregido si ese label cambió a display:block
        pattern_block = rf'<label[^>]*for="{re.escape(for_value)}"[^>]*style="[^"]*display\s*:\s*block[^"]*"[^>]*>'
        # También buscar si se eliminó el hidden o display:none
        pattern_no_hidden = rf'<label[^>]*for="{re.escape(for_value)}"[^>]*(?!style="[^"]*display\s*:\s*none)(?!class="[^"]*visually-hidden)(?!hidden)[^>]*>'
        
        needs_fix = False
        if re.search(pattern_block, corrected, re.IGNORECASE):
            needs_fix = True
        elif re.search(pattern_no_hidden, corrected, re.IGNORECASE):
            # Verificar que no tenga display:none ni visually-hidden en el corregido
            corrected_label_match = re.search(
                rf'<label[^>]*for="{re.escape(for_value)}"[^>]*>.*?</label>',
                corrected,
                re.DOTALL | re.IGNORECASE
            )
            if corrected_label_match:
                corrected_label_full = corrected_label_match.group(0)
                if 'display:none' not in corrected_label_full.lower() and 'visually-hidden' not in corrected_label_full.lower() and 'hidden' not in corrected_label_full.lower():
                    needs_fix = True
        
        if needs_fix:
            # El LLM cambió display:none/hidden a visible - revertirlo
            corrected_label_match = re.search(
                rf'<label[^>]*for="{re.escape(for_value)}"[^>]*>.*?</label>',
                corrected,
                re.DOTALL | re.IGNORECASE
            )
            if corrected_label_match:
                corrected_label_full = corrected_label_match.group(0)
                # Extraer el atributo for y el contenido
                label_id_match = re.search(r'for="([^"]+)"', corrected_label_full)
                label_content_match = re.search(r'<label[^>]*>(.*?)</label>', corrected_label_full, re.DOTALL)
                
                if label_id_match and label_content_match:
                    new_label = f'<label for="{label_id_match.group(1)}" class="visually-hidden">{label_content_match.group(1).strip()}</label>'
                    corrected = corrected.replace(corrected_label_full, new_label)
                    print(f"  ⚠️ Detectado cambio que rompe responsive: label con display:block revertido a visually-hidden")
    
    return corrected


def _apply_automatic_accessibility_fixes(template_content: Optional[str]) -> Optional[str]:
    """
    Aplica correcciones automáticas de accesibilidad comunes que el LLM podría no hacer sistemáticamente.
    
    Correcciones aplicadas:
    1. Añade role="img" a elementos <i> y <nb-icon> que tienen aria-label pero no tienen role
    2. Añade lang attribute a <html> si falta
    3. Añade aria-label a elementos role="progressbar" que no lo tienen
    """
    if not template_content:
        return template_content
    
    import re
    corrected = template_content
    
    # 1. Añadir role="img" a <i> con aria-label pero sin role
    # Patrón: <i ... aria-label="..." ...> (sin role)
    pattern_i_with_aria = r'(<i\s+[^>]*aria-label="[^"]*"[^>]*?)(?<!role="[^"]*")(?<!role=\'[^\']*\')([^>]*>)'
    def add_role_to_i(match):
        full_tag = match.group(0)
        # Si ya tiene role, no hacer nada
        if 'role=' in full_tag:
            return full_tag
        # Añadir role="img" antes del cierre >
        return full_tag[:-1] + ' role="img">'
    
    # Buscar <i> con aria-label sin role
    i_tags = re.finditer(r'<i\s+[^>]*aria-label="[^"]*"[^>]*>', corrected)
    for match in list(i_tags):
        tag = match.group(0)
        if 'role=' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)
    
    # 2. Añadir role="img" a <nb-icon> con aria-label pero sin role
    # Buscar <nb-icon ... aria-label="..." ...> (sin role)
    nb_icon_tags = re.finditer(r'<nb-icon\s+[^>]*aria-label="[^"]*"[^>]*>', corrected)
    for match in list(nb_icon_tags):
        tag = match.group(0)
        if 'role=' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)
    
    # También manejar [attr.aria-label] (binding dinámico)
    nb_icon_tags_dynamic = re.finditer(r'<nb-icon\s+[^>]*\[attr\.aria-label\]="[^"]*"[^>]*>', corrected)
    for match in list(nb_icon_tags_dynamic):
        tag = match.group(0)
        if 'role=' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)
    
    # 3. Añadir lang attribute a <html> si falta
    if '<html' in corrected and 'lang=' not in corrected.split('<html')[1].split('>')[0]:
        corrected = re.sub(r'(<html)([^>]*>)', r'\1 lang="en"\2', corrected, count=1)
    
    # 4. Añadir aria-label a elementos con role="progressbar" que no lo tienen
    progressbar_pattern = r'(<[^>]*\s+role="progressbar"[^>]*?)(?<!aria-label="[^"]*")(?<!aria-labelledby="[^"]*")([^>]*>)'
    def add_aria_to_progressbar(match):
        full_tag = match.group(0)
        # Si ya tiene aria-label o aria-labelledby, no hacer nada
        if 'aria-label=' in full_tag or 'aria-labelledby=' in full_tag:
            return full_tag
        # Extraer el valor de aria-valuenow si existe para crear un label descriptivo
        valuenow_match = re.search(r'aria-valuenow="([^"]*)"', full_tag)
        valuenow = valuenow_match.group(1) if valuenow_match else ""
        label_text = f"Progress: {valuenow}%" if valuenow else "Progress indicator"
        # Añadir aria-label antes del cierre >
        return full_tag[:-1] + f' aria-label="{label_text}">'
    
    progressbar_tags = re.finditer(r'<[^>]*\s+role="progressbar"[^>]*>', corrected)
    for match in list(progressbar_tags):
        tag = match.group(0)
        if 'aria-label=' not in tag and 'aria-labelledby=' not in tag:
            # Crear un label descriptivo
            valuenow_match = re.search(r'aria-valuenow="([^"]*)"', tag)
            valuenow = valuenow_match.group(1) if valuenow_match else ""
            label_text = f"Progress: {valuenow}%" if valuenow else "Progress indicator"
            corrected = corrected.replace(tag, tag[:-1] + f' aria-label="{label_text}">', 1)
    
    return corrected


def _fix_basic_syntax_errors(template_content: Optional[str]) -> Optional[str]:
    """
    Corrige errores básicos de sintaxis HTML comunes que pueden introducirse por el LLM.
    Específicamente corrige atributos sin comillas de cierre: attr="value> -> attr="value">
    """
    if not template_content:
        return template_content
    
    import re
    
    corrected = template_content
    
    # Estrategia: procesar línea por línea y corregir atributos mal cerrados
    lines = corrected.split('\n')
    fixed_lines = []
    
    for line in lines:
        fixed_line = line
        
        # 1. Corregir atributos que terminan con > sin comilla de cierre
        # Ejemplos:
        #   aria-label="texto>  -> aria-label="texto">
        #   style="color: #000000 !important;>  -> style="color: #000000 !important;">
        #   for="email>  -> for="email">
        
        # Buscar todos los atributos en la línea: attr="valor>
        # Patrón: palabra-attr="cualquier-cosa-que-no-contenga-comillas>
        # Pero excluir template references (#ref) que no usan comillas
        
        # Enfoque: buscar patrones específicos de atributos mal cerrados
        # Caso 1: attr="texto> donde texto no contiene comillas
        # Usar un patrón que capture el atributo, el =", el valor, y el >
        # y luego añadir la comilla antes del >
        
        def fix_unclosed_attr_in_line(text):
            """Corrige atributos sin comilla de cierre en una línea"""
            result = text
            
            # Buscar patrones: attr="valor> donde el > está inmediatamente después del valor
            # Esto incluye tanto atributos normales como bindings de Angular
            
            # Patrón 1: Atributos normales: attr="valor>
            # También captura bindings de Angular: [attr]="expresion>, (event)="handler()>, etc.
            # El patrón debe capturar: nombre-attr="valor-contenido>
            # Donde valor-contenido puede tener espacios, caracteres especiales, expresiones de Angular, etc.
            
            # Patrón mejorado que captura también bindings de Angular
            # Busca: (event)="...>, [attr]="...>, *directiva="...>, etc.
            pattern = r'([(\[\*#]?[\w-]+(?:\([^)]*\))?[\]\)]?)="([^"]*?)([^">])\s*>'
            
            def replace_attr(match):
                attr_name = match.group(1)
                attr_value = match.group(2)
                last_char = match.group(3)
                
                # Verificar que no sea un template reference (#ref)
                if attr_name.startswith('#'):
                    return match.group(0)
                
                # Si el valor no está vacío, añadir comilla antes del >
                return f'{attr_name}="{attr_value}{last_char}">'
            
            result = re.sub(pattern, replace_attr, result)
            
            # Casos específicos más comunes
            # Corregir: style="...!important;> -> style="...!important;">
            result = re.sub(r'(style="[^"]*?)\s*!important\s*;>', r'\1 !important;">', result)
            # Corregir: style="color: #000000> -> style="color: #000000;">
            result = re.sub(r'(style="[^"]*?[^";])\s*>', r'\1;">', result)
            
            # Corregir atributos data-*: data-bs-target="#modal>texto -> data-bs-target="#modal">texto
            # Este patrón captura atributos que terminan justo antes de una palabra (no antes de >)
            result = re.sub(r'(data-[\w-]+="[^"]*?)>([A-Za-z])', r'\1">\2', result)
            
            # Corregir otros atributos: attr="valor> -> attr="valor">
            # Pero evitar duplicar comillas si ya hay una
            result = re.sub(r'([\w-]+)="([^"]*?[^"])\s*>(?!")', r'\1="\2">', result)
            
            return result
        
        fixed_line = fix_unclosed_attr_in_line(fixed_line)
        
        # 2. Corregir template references (#ref) que tienen comillas incorrectas
        # Template references NO deben tener comillas: #stepper"> -> #stepper>
        fixed_line = re.sub(r'#(\w+)">', r'#\1>', fixed_line)
        fixed_line = re.sub(r'#(\w+)\s*">', r'#\1>', fixed_line)
        
        # 3. Casos específicos conocidos
        fixed_line = fixed_line.replace('#stepper">', '#stepper>')
        fixed_line = fixed_line.replace('#picker">', '#picker>')
        fixed_line = fixed_line.replace('#drawer">', '#drawer>')
        
        fixed_lines.append(fixed_line)
    
    corrected = '\n'.join(fixed_lines)
    
    return corrected


def _fix_angular_aria_syntax(template_content: Optional[str]) -> Optional[str]:
    """Corrige la sintaxis de atributos ARIA en templates Angular para usar [attr.aria-*] con binding"""
    if not template_content:
        return template_content
    
    import re
    
    # Patrón para encontrar aria-* con binding de interpolación {{ }}
    # Ejemplo: aria-pressed="{{condicion}}" -> [attr.aria-pressed]="condicion"
    pattern_interpolation = r'aria-([a-z-]+)="{{([^}]+)}}"'
    def replace_interpolation(match):
        attr_name = match.group(1)
        expression = match.group(2).strip()
        return f'[attr.aria-{attr_name}]="{expression}"'
    
    corrected = re.sub(pattern_interpolation, replace_interpolation, template_content)
    
    # Patrón para encontrar aria-* con interpolación en strings
    # Ejemplo: aria-label="Texto {{variable}}" -> [attr.aria-label]="'Texto ' + variable"
    pattern_string_interpolation = r'aria-([a-z-]+)="([^"]*)\{\{([^}]+)\}\}([^"]*)"'
    def replace_string_interpolation(match):
        attr_name = match.group(1)
        before = match.group(2)
        expression = match.group(3).strip()
        after = match.group(4)
        # Construir expresión concatenada
        parts = []
        if before:
            parts.append(f"'{before}'")
        parts.append(expression)
        if after:
            parts.append(f"'{after}'")
        return f'[attr.aria-{attr_name}]="{" + ".join(parts)}"'
    
    corrected = re.sub(pattern_string_interpolation, replace_string_interpolation, corrected)
    
    return corrected


def _apply_changes_map(changes_map: List[Dict], project_root: Path) -> int:
    """Aplica el mapa de cambios al código fuente real"""
    applied_count = 0
    for change_entry in changes_map:
        changes = change_entry.get("changes", {})
        for file_type, file_change in changes.items():
            try:
                target_path = Path(file_change["path"])
                target_path.write_text(file_change["corrected"], encoding="utf-8")
                applied_count += 1
            except Exception as e:
                print(f"  ⚠️ Error aplicando cambio en {file_change['path']}: {e}")
    return applied_count


def _revert_changes(changes_map: List[Dict], project_root: Path) -> None:
    """Revierte los cambios aplicados restaurando el contenido original"""
    for change_entry in changes_map:
        changes = change_entry.get("changes", {})
        for file_type, file_change in changes.items():
            try:
                target_path = Path(file_change["path"])
                target_path.write_text(file_change["original"], encoding="utf-8")
            except Exception as e:
                print(f"  ⚠️ Error revirtiendo cambio en {file_change['path']}: {e}")


def _verify_angular_build(project_root: Path) -> Tuple[bool, bool]:
    """
    Verifica que el proyecto Angular compile correctamente ejecutando ng build.
    
    Returns:
        Tuple de (éxito de compilación, disponibilidad de verificación)
        Si la verificación no está disponible (ng no encontrado), retorna (True, False)
        para no bloquear el proceso.
    """
    # Detectar si es un workspace multi-proyecto
    default_project = _get_default_project_name(project_root)
    project_arg = [default_project] if default_project else []
    if default_project:
        print(f"  → Workspace multi-proyecto detectado, compilando: {default_project}")
    
    # Estrategia 1: Intentar con npm run build (más común en proyectos Angular)
    package_json = project_root / "package.json"
    if package_json.exists():
        try:
            with open(package_json, 'r', encoding='utf-8') as f:
                package_data = json.load(f)
                scripts = package_data.get("scripts", {})
                if "build" in scripts:
                    print("  → Usando 'npm run build' para verificar compilación...")
                    result = subprocess.run(
                        ["npm", "run", "build"],
                        cwd=str(project_root),
                        capture_output=True,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        timeout=300
                    )
                    if result.returncode == 0:
                        return True, True
                    else:
                        # Mostrar errores de compilación si hay
                        if result.stderr:
                            print(f"  Errores de compilación:\n{result.stderr[:500]}")
                        return False, True
        except Exception as e:
            pass
    
    # Estrategia 2: Intentar con ng directamente
    try:
        build_cmd = ["ng", "build"] + project_arg + ["--configuration", "production"]
        result = subprocess.run(
            build_cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=300
        )
        if result.returncode == 0:
            return True, True
        else:
            if result.stderr:
                print(f"  Errores de compilación:\n{result.stderr[:500]}")
            return False, True
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        print("  ⚠️ Timeout al compilar el proyecto")
        return False, True
    except Exception:
        pass
    
    # Estrategia 3: Intentar con npx
    try:
        build_cmd = ["npx", "-y", "@angular/cli", "build"] + project_arg + ["--configuration", "production"]
        result = subprocess.run(
            build_cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=300
        )
        if result.returncode == 0:
            return True, True
        else:
            if result.stderr:
                print(f"  Errores de compilación:\n{result.stderr[:500]}")
            return False, True
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        print("  ⚠️ Timeout al compilar el proyecto")
        return False, True
    except Exception:
        pass
    
    # Estrategia 4: Intentar con node_modules/.bin/ng
    node_modules_ng = project_root / "node_modules" / ".bin" / "ng"
    if node_modules_ng.exists():
        try:
            # En Windows, puede ser ng.cmd
            ng_cmd = str(node_modules_ng)
            if not ng_cmd.endswith('.cmd') and (project_root / "node_modules" / ".bin" / "ng.cmd").exists():
                ng_cmd = str(project_root / "node_modules" / ".bin" / "ng.cmd")
            
            build_cmd = [ng_cmd, "build"] + project_arg + ["--configuration", "production"]
            result = subprocess.run(
                build_cmd,
                cwd=str(project_root),
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=300
            )
            if result.returncode == 0:
                return True, True
            else:
                if result.stderr:
                    print(f"  Errores de compilación:\n{result.stderr[:500]}")
                return False, True
        except Exception as e:
            print(f"  ⚠️ Error ejecutando ng desde node_modules: {e}")
    
    # Si ninguna estrategia funciona, asumir que no se puede verificar
    print("  ⚠️ No se pudo ejecutar ng build (ng no encontrado en PATH, npx no disponible, o node_modules no encontrado)")
    print("  → Continuando sin verificación de compilación")
    return True, False  # Retornar (True, False) para indicar que no se pudo verificar pero no bloquear


def _compile_and_get_errors(project_root: Path) -> Dict:
    """
    Compila el proyecto Angular y retorna los errores de compilación si los hay.
    
    Returns:
        Dict con:
        - success: bool - Si la compilación fue exitosa
        - verification_available: bool - Si se pudo verificar la compilación
        - errors: List[str] - Lista de errores de compilación
        - output: str - Salida completa de la compilación
    """
    errors = []
    output = ""
    success = True
    verification_available = False
    
    # Siempre ejecutar build para capturar errores, independientemente de _verify_angular_build
    try:
        # Ejecutar build y capturar stderr y stdout
        package_json = project_root / "package.json"
        if package_json.exists():
            try:
                with open(package_json, 'r', encoding='utf-8') as f:
                    package_data = json.load(f)
                    scripts = package_data.get("scripts", {})
                    if "build" in scripts:
                        result = subprocess.run(
                            ["npm", "run", "build"],
                            cwd=str(project_root),
                            capture_output=True,
                            text=True,
                            timeout=300
                        )
                        output = result.stderr + result.stdout
                        verification_available = True
                        # Parsear errores incluso si returncode == 0 (puede haber errores de TypeScript)
                        errors = _parse_angular_errors(output)
                        if errors:
                            success = False
                            print(f"  → Build completó pero se encontraron {len(errors)} errores, parseando...")
                        elif result.returncode != 0:
                            success = False
                            print(f"  → Build falló, parseando errores...")
            except Exception as e:
                print(f"  ⚠️ Error ejecutando npm run build: {e}")
        
        # Si no se obtuvieron errores o no hay script build, intentar con ng build
        if not verification_available or (not errors and not success):
            try:
                # Detectar si es un workspace multi-proyecto
                default_project = _get_default_project_name(project_root)
                build_cmd = ["ng", "build"]
                if default_project:
                    build_cmd.append(default_project)
                    print(f"  → Workspace multi-proyecto detectado, compilando proyecto: {default_project}")
                
                result = subprocess.run(
                    build_cmd,
                    cwd=str(project_root),
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=300
                )
                output = result.stderr + result.stdout
                verification_available = True
                # Parsear errores incluso si returncode == 0 (puede haber errores de TypeScript)
                if not errors:  # Solo parsear si no se obtuvieron errores antes
                    errors = _parse_angular_errors(output)
                    if errors:
                        success = False
                        print(f"  → Build completó pero se encontraron {len(errors)} errores, parseando...")
                    elif result.returncode != 0:
                        success = False
                        print(f"  → Build falló, parseando errores...")
            except Exception as e:
                print(f"  ⚠️ Error ejecutando ng build: {e}")
    except Exception as e:
        print(f"  ⚠️ Error general en _compile_and_get_errors: {e}")
    
    # Si no se pudo verificar, usar _verify_angular_build como fallback
    if not verification_available:
        result = _verify_angular_build(project_root)
        success, verification_available = result
    
    return {
        "success": success,
        "verification_available": verification_available,
        "errors": errors,
        "output": output
    }


def _parse_angular_errors(build_output: str) -> List[str]:
    """Parsea los errores de compilación de Angular del output"""
    errors = []
    lines = build_output.split('\n')
    
    current_error = []
    in_error_block = False
    
    # Primero, buscar errores específicos de TypeScript/Angular que pueden aparecer incluso cuando el build "completa"
    for i, line in enumerate(lines):
        # Buscar líneas que indican errores (más específico)
        # Incluir errores que empiezan con ./src/ (webpack errors)
        # También buscar "Module not found" o "Can't resolve" directamente
        # Buscar patrones de error TS y NG incluso sin el prefijo "ERROR"
        is_error_line = (
            'ERROR' in line.upper() or 
            'error TS' in line.lower() or 
            'error NG' in line.lower() or 
            (line.strip().startswith('./src/') and ('Error:' in line or 'Error' in line or 'Module not found' in line or "Can't resolve" in line)) or
            'Module not found' in line or 
            "Can't resolve" in line or 
            'Cannot find module' in line or
            # Patrones adicionales para errores de TypeScript
            (line.strip().startswith('src/') and 'error TS' in line.lower()) or
            (line.strip().startswith('Error:') and ('TS' in line or 'NG' in line))
        )
        
        if is_error_line:
            if current_error:
                errors.append('\n'.join(current_error))
                current_error = []
            current_error.append(line)
            in_error_block = True
        elif in_error_block:
            # Continuar agregando líneas del error hasta encontrar una línea vacía o un nuevo error
            if line.strip() == '' and current_error:
                # Línea vacía puede indicar fin del error, pero continuar si hay contexto
                if len(current_error) > 1:
                    current_error.append(line)
                else:
                    if current_error:
                        errors.append('\n'.join(current_error))
                        current_error = []
                        in_error_block = False
            elif (line.strip().startswith('src/') or line.strip().startswith('./src/') or 
                  ':' in line or line.strip().startswith('Error occurs') or 
                  'error TS' in line.lower() or 'error NG' in line.lower() or
                  'Cannot find module' in line or "Can't resolve" in line or
                  'imports:' in line or 'import {' in line):
                current_error.append(line)
            elif current_error and (line.strip() or 'at ' in line or '^' in line):
                # Líneas de contexto del error (stack trace, ubicación, etc.)
                current_error.append(line)
            else:
                # Fin del bloque de error
                if current_error:
                    errors.append('\n'.join(current_error))
                    current_error = []
                    in_error_block = False
    
    if current_error:
        errors.append('\n'.join(current_error))
    
    # Filtrar errores vacíos
    errors = [e for e in errors if e.strip()]
    
    return errors[:20]  # Limitar a 20 errores


def _fix_compilation_errors(errors: List[str], project_root: Path, client) -> List[Dict]:
    """
    Corrige errores de compilación usando LLM y correcciones automáticas.
    
    Returns:
        Lista de correcciones a aplicar
    """
    if not errors:
        return []
    
    fixes = []
    
    # Primero, aplicar correcciones automáticas para errores comunes de módulos faltantes
    import re
    print(f"  → Analizando {len(errors)} errores para correcciones automáticas...")
    for i, error in enumerate(errors):
        # Buscar errores de "Module not found" o "Cannot find module"
        if 'Module not found' in error or 'Cannot find module' in error or "Can't resolve" in error:
            print(f"    Error {i+1}: Detectado error de módulo faltante")
            print(f"      Primeras líneas: {error.split(chr(10))[0][:150]}...")
            
            # Extraer el nombre del módulo y la ruta del archivo
            module_match = re.search(r"Can't resolve '([^']+)'|Cannot find module '([^']+)'|Module not found.*?'([^']+)'", error)
            file_match = re.search(r'(?:\./)?src/([^\s:]+\.(?:ts|html|scss|css|sass))', error)
            
            if module_match:
                module_name = module_match.group(1) or module_match.group(2) or module_match.group(3)
                print(f"      Módulo detectado: {module_name}")
            else:
                print(f"      ⚠️ No se pudo extraer el nombre del módulo")
                module_name = None
            
            if file_match:
                file_path = 'src/' + file_match.group(1)
                print(f"      Archivo detectado: {file_path}")
            else:
                print(f"      ⚠️ No se pudo extraer la ruta del archivo")
                file_path = None
            
            if module_match and file_match and module_name:
                full_path = project_root / file_path
                
                if full_path.exists():
                    print(f"  → Aplicando corrección automática para módulo faltante: {module_name} en {file_path}")
                    try:
                        content = full_path.read_text(encoding='utf-8')
                        corrected_content = _auto_fix_missing_module(content, module_name)
                        
                        if corrected_content != content:
                            # Aplicar inmediatamente
                            full_path.write_text(corrected_content, encoding='utf-8')
                            fixes.append({
                                "path": file_path,
                                "original": content,
                                "corrected": corrected_content
                            })
                            print(f"    ✓ Corrección automática aplicada y guardada en {file_path}")
                        else:
                            print(f"    ⚠️ No se detectaron cambios en {file_path}")
                    except Exception as e:
                        print(f"    ⚠️ Error en corrección automática: {e}")
                        import traceback
                        traceback.print_exc()
                else:
                    print(f"    ⚠️ Archivo no existe: {full_path}")
            else:
                print(f"    ⚠️ No se pudo extraer módulo o archivo del error")
    
    # Primero, intentar instalar módulos faltantes automáticamente
    missing_modules = []
    for error in errors:
        # Buscar errores de "Module not found" o "Cannot find module"
        if 'Module not found' in error or 'Cannot find module' in error or "Can't resolve" in error:
            # Extraer el nombre del módulo
            module_match = re.search(r"Can't resolve '([^']+)'|Cannot find module '([^']+)'", error)
            if module_match:
                module_name = module_match.group(1) or module_match.group(2)
                if module_name and module_name not in missing_modules:
                    missing_modules.append(module_name)
    
    # Intentar instalar módulos faltantes
    if missing_modules:
        print(f"  → Detectados {len(missing_modules)} módulos faltantes, intentando instalar...")
        for module in missing_modules:
            try:
                print(f"    → Instalando {module}...")
                result = subprocess.run(
                    ["npm", "install", module],
                    cwd=str(project_root),
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=120
                )
                if result.returncode == 0:
                    print(f"    ✓ {module} instalado correctamente")
                else:
                    print(f"    ⚠️ No se pudo instalar {module}: {result.stderr[:200]}")
            except Exception as e:
                print(f"    ⚠️ Error instalando {module}: {e}")
    
    # Agrupar errores por archivo
    errors_by_file = {}
    for error in errors:
        # Extraer ruta del archivo del error
        file_path = None
        for line in error.split('\n'):
            # Buscar patrones de ruta de archivo en el error
            if 'src/' in line or './src/' in line or 'projects/' in line:
                import re
                # Patrones posibles:
                # - src/path/to/file.ts
                # - ./src/path/to/file.ts
                # - projects/xxx/src/path/to/file.ts
                match = re.search(
                    r'((?:\./)?(?:projects/[^\s:]+/)?src/[^\s:]+\.(ts|html|scss|css|sass))',
                    line
                )
                if match:
                    potential_path = match.group(1)
                    if potential_path.startswith('./'):
                        potential_path = potential_path[2:]
                    full_path = project_root / potential_path
                    if full_path.exists():
                        file_path = potential_path
                        break
        
        if file_path:
            if file_path not in errors_by_file:
                errors_by_file[file_path] = []
            errors_by_file[file_path].append(error)
        else:
            # Si no se encontró archivo, agregar a "unknown" para debugging
            if "unknown" not in errors_by_file:
                errors_by_file["unknown"] = []
            errors_by_file["unknown"].append(error)
    
    # Corregir errores archivo por archivo
    print(f"  → Encontrados errores en {len([f for f in errors_by_file.keys() if f != 'unknown'])} archivo(s)")
    if "unknown" in errors_by_file:
        print(f"  ⚠️ {len(errors_by_file['unknown'])} error(es) no se pudieron asociar a un archivo específico")
    
    for file_path, file_errors in list(errors_by_file.items())[:10]:  # Limitar a 10 archivos
        if file_path == "unknown":
            print(f"  ⚠️ Saltando {len(file_errors)} error(es) sin archivo asociado")
            continue
            
        try:
            full_path = project_root / file_path
            if not full_path.exists():
                continue
                
            original_content = full_path.read_text(encoding='utf-8')
            errors_text = '\n\n'.join(file_errors[:3])  # Limitar a 3 errores por archivo
            
            # Usar LLM para corregir errores
            system_message = "Eres un experto en Angular y TypeScript. Corrige los errores de compilación sin cambiar la funcionalidad."
            
            # Detectar si hay errores de módulos faltantes
            has_missing_module = 'Module not found' in errors_text or 'Cannot find module' in errors_text or "Can't resolve" in errors_text
            
            if has_missing_module:
                # Extraer el nombre del módulo faltante del error
                import re
                module_name = None
                module_match = re.search(r"Can't resolve '([^']+)'|Cannot find module '([^']+)'|Module not found.*'([^']+)'", errors_text)
                if module_match:
                    module_name = module_match.group(1) or module_match.group(2) or module_match.group(3)
                
                prompt = f"""
Corrige los siguientes errores de compilación de Angular en el archivo {file_path}:

Errores:
{errors_text}

IMPORTANTE: El módulo '{module_name if module_name else "desconocido"}' no se puede encontrar o no existe en npm.
DEBES hacer lo siguiente:
1. COMENTAR o ELIMINAR el import del módulo faltante
2. COMENTAR o ELIMINAR todos los usos del módulo en el código (en imports del @Component, en el código, etc.)
3. Si el módulo se usa en el array de imports del @Component, ELIMÍNALO de ese array
4. Añade un comentario explicativo: // Módulo no disponible: {module_name if module_name else "módulo faltante"}

Ejemplo:
- Si hay: import {{CKEditorModule}} from "@angular/ckeditor5-angular";
- Cambia a: // import {{CKEditorModule}} from "@angular/ckeditor5-angular"; // Módulo no disponible
- Y elimina CKEditorModule del array de imports del @Component

Contenido actual del archivo:
```typescript
{original_content[:3000]}
```

Corrige SOLO los errores de compilación. COMENTA o ELIMINA el import y TODOS sus usos.
Retorna el código corregido completo sin el módulo faltante.
"""
            else:
                prompt = f"""
Corrige los siguientes errores de compilación de Angular en el archivo {file_path}:

Errores:
{errors_text}

Contenido actual del archivo:
```typescript
{original_content[:3000]}
```

Corrige SOLO los errores de compilación. Mantén toda la funcionalidad y lógica existente.
Retorna el código corregido completo.
"""
            
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )
            
            corrected_content = response.choices[0].message.content.strip()
            log_openai_call(prompt=prompt, response=corrected_content, model="gpt-4o", call_type="angular_compilation_fix")
            
            # Limpiar el código corregido (remover markdown si existe)
            if corrected_content.startswith('```'):
                parts = corrected_content.split('```')
                if len(parts) >= 3:
                    # Extraer el contenido entre los bloques de código
                    code_block = parts[1]
                    if code_block.startswith('typescript') or code_block.startswith('ts') or code_block.startswith('html'):
                        code_block = code_block.split('\n', 1)[1] if '\n' in code_block else ''
                    corrected_content = code_block.strip()
                else:
                    # Si no hay cierre, intentar extraer de otra forma
                    corrected_content = corrected_content.replace('```typescript', '').replace('```ts', '').replace('```html', '').replace('```', '').strip()
            
            corrected_content = corrected_content.strip()
            
            if corrected_content and corrected_content != original_content.strip():
                print(f"    ✓ Corrección generada para {file_path}")
                fixes.append({
                    "path": str(full_path),
                    "original": original_content,
                    "corrected": corrected_content,
                    "errors": file_errors
                })
            else:
                print(f"    ⚠️ No se generó corrección válida para {file_path}")
        except Exception as e:
            print(f"  ⚠️ Error corrigiendo {file_path}: {e}")
            import traceback
            traceback.print_exc()
    
    return fixes


def _auto_fix_missing_module(content: str, module_name: str) -> str:
    """Corrige automáticamente un módulo faltante comentando el import y eliminando sus usos"""
    import re
    
    lines = content.split('\n')
    corrected_lines = []
    module_short_names = []
    
    # Extraer el nombre corto del módulo (ej: CKEditorModule de @angular/ckeditor5-angular)
    import_pattern = rf'import\s+\{{([^}}]+)\}}\s+from\s+["\']{re.escape(module_name)}["\']'
    import_match = re.search(import_pattern, content)
    if import_match:
        imports_str = import_match.group(1)
        # Puede haber múltiples imports separados por comas
        module_short_names = [name.strip() for name in imports_str.split(',')]
        print(f"      → Módulos detectados en import: {module_short_names}")
    else:
        print(f"      ⚠️ No se encontró el import de {module_name}")
    
    import_commented = False
    imports_removed = False
    
    for i, line in enumerate(lines):
        original_line = line
        
        # Comentar el import del módulo faltante
        if module_name in line and 'import' in line and 'from' in line:
            # Comentar la línea completa
            if not line.strip().startswith('//'):
                # Preservar la indentación
                indent = len(line) - len(line.lstrip())
                corrected_lines.append(' ' * indent + f"// {line.strip()} // Módulo no disponible: {module_name}")
                import_commented = True
                print(f"      → Import comentado: {line.strip()[:60]}...")
            else:
                corrected_lines.append(line)
        # Eliminar el módulo del array de imports del @Component
        elif module_short_names and any(name in line for name in module_short_names):
            # Buscar si esta línea contiene el array de imports
            if 'imports:' in line or ('imports' in line and '[' in line):
                # Eliminar cada módulo del array
                original_line_for_log = line
                for module_short_name in module_short_names:
                    if module_short_name in line:
                        # Eliminar el módulo del array con diferentes patrones
                        # Patrón 1: , ModuleName,
                        line = re.sub(rf',\s*{re.escape(module_short_name)}\s*,', ',', line)
                        # Patrón 2: , ModuleName]
                        line = re.sub(rf',\s*{re.escape(module_short_name)}\s*\]', ']', line)
                        # Patrón 3: [ModuleName,
                        line = re.sub(rf'\[\s*{re.escape(module_short_name)}\s*,', '[', line)
                        # Patrón 4: [ModuleName]
                        line = re.sub(rf'\[\s*{re.escape(module_short_name)}\s*\]', '[]', line)
                        # Limpiar comas dobles
                        line = re.sub(r',\s*,', ',', line)
                        # Limpiar espacios extra alrededor de comas
                        line = re.sub(r',\s+', ', ', line)
                if line != original_line_for_log:
                    imports_removed = True
                    print(f"      → Módulo eliminado del array imports: {original_line_for_log.strip()[:60]}...")
                corrected_lines.append(line)
            else:
                corrected_lines.append(line)
        else:
            corrected_lines.append(line)
    
    if not import_commented:
        print(f"      ⚠️ No se comentó ningún import")
    if not imports_removed:
        print(f"      ⚠️ No se eliminó ningún módulo del array imports")
    
    return '\n'.join(corrected_lines)


def _apply_compilation_fixes(fixes: List[Dict], project_root: Path) -> None:
    """Aplica las correcciones de compilación"""
    for fix in fixes:
        try:
            target_path = Path(fix["path"])
            target_path.write_text(fix["corrected"], encoding="utf-8")
        except Exception as e:
            print(f"  ⚠️ Error aplicando corrección en {fix['path']}: {e}")


def _start_angular_dev_server(project_root: Path, port: int = 4200, wait_for_ready: bool = False):
    """Inicia el servidor de desarrollo Angular (ng serve) en el puerto especificado
    
    Args:
        project_root: Ruta al proyecto Angular
        port: Puerto donde iniciar el servidor (default: 4200)
        wait_for_ready: Si True, inicia el servidor en background y retorna el proceso. Si False, ejecuta en foreground.
    
    Returns:
        subprocess.Popen si wait_for_ready=True, None si wait_for_ready=False
    """
    import socket
    
    # Verificar si el puerto está disponible
    def is_port_available(port_num: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('localhost', port_num))
                return True
            except OSError:
                return False
    
    # Verificar puerto 4200
    if not is_port_available(port):
        print(f"  ⚠️ El puerto {port} está ocupado.")
        response = input(f"  ¿Deseas usar otro puerto? (s/n): ")
        if response.lower() == 's':
            # Buscar puerto disponible
            for p in range(4201, 4210):
                if is_port_available(p):
                    port = p
                    print(f"  → Usando puerto {port}")
                    break
            else:
                print("  ⚠️ No se encontró un puerto disponible. Usando puerto por defecto.")
                port = 4200
        else:
            print("  → Intentando usar el puerto 4200 de todas formas...")
    
    # Función auxiliar para verificar si un comando existe
    def command_exists(cmd):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=2,
                check=False
            )
            # Si el comando existe, retornará 0 o 1 (no FileNotFoundError)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        except Exception:
            return False
    
    # Estrategia 1: Intentar con npm run start primero
    package_json = project_root / "package.json"
    if package_json.exists():
        try:
            with open(package_json, 'r', encoding='utf-8') as f:
                package_data = json.load(f)
                scripts = package_data.get("scripts", {})
                if "start" in scripts:
                    # Verificar que npm existe
                    if command_exists(["npm", "--version"]):
                        print(f"  → Iniciando servidor con 'npm start' en puerto {port}...")
                        print("  Presiona Ctrl+C para detener el servidor.")
                        try:
                            # Modificar el script start para usar el puerto específico si es necesario
                            subprocess.run(
                                ["npm", "start", "--", "--port", str(port)],
                                cwd=str(project_root),
                                check=False
                            )
                            return
                        except KeyboardInterrupt:
                            print("\n  Servidor detenido por el usuario.")
                            return
                        except Exception as e:
                            print(f"  ⚠️ Error con 'npm start': {e}")
        except Exception:
            pass
    
    # Estrategia 2: Intentar con ng serve directamente
    if command_exists(["ng", "version"]):
        try:
            print(f"  → Iniciando servidor con 'ng serve --port {port}'...")
            if wait_for_ready:
                process = subprocess.Popen(
                    ["ng", "serve", "--port", str(port)],
                    cwd=str(project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding='utf-8',
                    errors='replace'
                )
                return process
            else:
                print("  Presiona Ctrl+C para detener el servidor.")
                subprocess.run(
                    ["ng", "serve", "--port", str(port), "--open"],
                    cwd=str(project_root),
                    check=False
                )
                return None
        except KeyboardInterrupt:
            if process:
                process.terminate()
            print("\n  Servidor detenido por el usuario.")
            return None
        except Exception as e:
            print(f"  ⚠️ Error con 'ng serve': {e}")
    
    # Estrategia 3: Intentar con npx
    if command_exists(["npx", "--version"]):
        try:
            print(f"  → Iniciando servidor con 'npx ng serve --port {port}'...")
            if wait_for_ready:
                process = subprocess.Popen(
                    ["npx", "-y", "@angular/cli", "serve", "--port", str(port)],
                    cwd=str(project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding='utf-8',
                    errors='replace'
                )
                return process
            else:
                print("  Presiona Ctrl+C para detener el servidor.")
                subprocess.run(
                    ["npx", "-y", "@angular/cli", "serve", "--port", str(port), "--open"],
                    cwd=str(project_root),
                    check=False
                )
                return None
        except KeyboardInterrupt:
            if process:
                process.terminate()
            print("\n  Servidor detenido por el usuario.")
            return None
        except Exception as e:
            print(f"  ⚠️ Error con 'npx ng serve': {e}")
    
    # Estrategia 4: Intentar con node_modules/.bin/ng (Windows y Unix)
    ng_cmd_path = None
    # En Windows, buscar .cmd primero
    if (project_root / "node_modules" / ".bin" / "ng.cmd").exists():
        ng_cmd_path = str(project_root / "node_modules" / ".bin" / "ng.cmd")
    elif (project_root / "node_modules" / ".bin" / "ng.bat").exists():
        ng_cmd_path = str(project_root / "node_modules" / ".bin" / "ng.bat")
    elif (project_root / "node_modules" / ".bin" / "ng").exists():
        # En Windows, puede necesitar ejecutarse con cmd /c
        import sys
        if sys.platform == "win32":
            ng_cmd_path = str(project_root / "node_modules" / ".bin" / "ng.cmd")
            if not Path(ng_cmd_path).exists():
                # Si no existe .cmd, intentar ejecutar el script directamente con node
                ng_script = project_root / "node_modules" / ".bin" / "ng"
                if ng_script.exists():
                    # Leer el shebang para ver cómo ejecutarlo
                    try:
                        with open(ng_script, 'r', encoding='utf-8') as f:
                            first_line = f.readline()
                            if first_line.startswith('#!'):
                                # Es un script, necesitamos ejecutarlo con node
                                ng_cmd_path = None  # Se manejará diferente
                    except Exception:
                        pass
    
    if ng_cmd_path and Path(ng_cmd_path).exists():
        try:
            print(f"  → Iniciando servidor con '{ng_cmd_path} serve --port {port} --open'...")
            print("  Presiona Ctrl+C para detener el servidor.")
            subprocess.run(
                [ng_cmd_path, "serve", "--port", str(port), "--open"],
                cwd=str(project_root),
                check=False
            )
            return
        except KeyboardInterrupt:
            print("\n  Servidor detenido por el usuario.")
            return
        except Exception as e:
            print(f"  ⚠️ Error ejecutando ng desde node_modules: {e}")
    
    print("  ⚠️ No se pudo iniciar el servidor (ng no encontrado en ninguna ubicación)")
    print(f"  → Puedes iniciarlo manualmente con: ng serve --port {port}")


def _write_if_changed(target_path: Path, new_content: Optional[str], original_content: str) -> bool:
    if new_content is None:
        return False
    if new_content.strip() == original_content.strip():
        return False
    target_path.write_text(new_content, encoding="utf-8")
    return True
