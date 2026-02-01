import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.io_utils import log_openai_call
from core.webdriver_setup import setup_driver
from core.analyzer import run_axe_analysis
from core.screenshot_handler import take_screenshots, create_screenshot_summary


def _normalize_react_html(html: str) -> str:
    """
    Normaliza HTML generado por React para poder compararlo con los componentes JSX.
    
    - Elimina atributos generados en runtime (data-react-*, etc.)
    - Colapsa espacios en blanco para hacer comparaciones más robustas.
    """
    if not html:
        return ""
    
    text = html
    # Quitar atributos "ruido" típicos de React en el DOM renderizado
    text = re.sub(r'\sdata-react[^= ]*="[^"]*"', "", text)
    # Normalizar espacios en blanco
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _jsx_contains_html_elements(jsx_content: str, html_snippet: str) -> bool:
    """
    Verifica si el JSX contiene los elementos HTML del snippet (ignorando atributos React).
    """
    if not jsx_content or not html_snippet:
        return False
    
    # Normalizar ambos
    normalized_jsx = _normalize_react_html(jsx_content)
    normalized_html = _normalize_react_html(html_snippet)
    
    # Buscar tags principales del snippet en el JSX
    tags = re.findall(r'<(\w+)', normalized_html)
    if not tags:
        return False
    
    # Verificar que todos los tags principales estén en el JSX
    for tag in tags:
        if f'<{tag}' not in normalized_jsx:
            return False
    
    return True


def detect_react_project(project_path: str) -> bool:
    """
    Detecta si un proyecto es React verificando package.json y dependencias.
    """
    try:
        project_root = Path(project_path)
        package_json = project_root / "package.json"
        
        if not package_json.exists():
            return False
        
        try:
            with open(package_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            dependencies = data.get("dependencies", {})
            dev_dependencies = data.get("devDependencies", {})
            all_deps = {**dependencies, **dev_dependencies}
            
            # Verificar si tiene React
            has_react = any(
                dep.lower().startswith('react') 
                for dep in all_deps.keys()
            )
            
            # También verificar si hay archivos JSX/TSX
            has_jsx = any(project_root.glob("**/*.jsx")) or any(project_root.glob("**/*.tsx"))
            
            return has_react or has_jsx
        except (json.JSONDecodeError, KeyError):
            return False
    except Exception:
        return False


def _has_react_dependencies(project_root: Path) -> bool:
    """Verifica si el proyecto tiene dependencias de React."""
    package_json = project_root / "package.json"
    if not package_json.exists():
        return False
    
    try:
        with open(package_json, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        return any('react' in k.lower() for k in deps.keys())
    except Exception:
        return False


def discover_react_components(source_roots: List[Path]) -> List[Path]:
    """
    Descubre todos los componentes React en el proyecto.
    
    Busca archivos .jsx, .tsx, y también .js/.ts que contengan JSX
    """
    components: List[Path] = []
    
    for root in source_roots:
        if not root.exists():
            print(f"[React + Axe] ⚠️ Directorio no existe: {root}")
            continue
        # No escanear node_modules
        if "node_modules" in str(root):
            continue
        
        # Buscar archivos JSX/TSX explícitos (SIEMPRE incluir estos)
        jsx_files = [p for p in root.glob("**/*.jsx") if "node_modules" not in str(p)]
        tsx_files = [p for p in root.glob("**/*.tsx") if "node_modules" not in str(p)]
        components.extend(jsx_files)
        components.extend(tsx_files)
        print(f"[React + Axe]   → Encontrados {len(jsx_files)} .jsx y {len(tsx_files)} .tsx")
        
        # SIEMPRE buscar también en .js/.ts que puedan contener JSX
        # Muchos proyectos React usan .js para componentes
        js_files = [p for p in root.glob("**/*.js") if "node_modules" not in str(p)]
        ts_files = [p for p in root.glob("**/*.ts") if "node_modules" not in str(p)]
        print(f"[React + Axe]   → Encontrados {len(js_files)} .js y {len(ts_files)} .ts (filtrando...)")
        
        # Filtrar archivos que claramente NO son componentes React
        skip_patterns = [
            '/config/', '/setup', 'setupTests', 'setupTests.js', 'setupTests.ts',
            'reportWebVitals', 'serviceWorker', 'registerServiceWorker',
            '/__tests__/', '/test/', '/tests/', '.test.js', '.test.ts', '.spec.js', '.spec.ts'
        ]
        
        js_components_found = 0
        for js_file in js_files:
            # Saltar archivos de configuración comunes (pero NO index.js - puede ser componente)
            if any(skip in str(js_file) for skip in skip_patterns):
                continue
            try:
                content = js_file.read_text(encoding="utf-8", errors="ignore")
                # Si el archivo es muy pequeño, probablemente no es un componente
                if len(content) < 30:
                    continue
                # Buscar indicadores de componente React (MUY permisivo)
                has_react_import = bool(re.search(r'import\s+.*from\s+["\']react["\']', content, re.IGNORECASE))
                has_jsx = bool(re.search(r'<[a-zA-Z]', content))  # Cualquier JSX
                has_return_jsx = bool(re.search(r'return\s+<', content) or re.search(r'return\s+\(', content))
                has_export = bool(re.search(r'export\s+(default\s+)?(function|const|class)', content))
                
                # Si importa React Y tiene JSX, es muy probable que sea un componente
                if has_react_import and (has_jsx or has_return_jsx):
                    components.append(js_file)
                    js_components_found += 1
                # O si exporta algo y tiene JSX
                elif has_export and has_jsx:
                    components.append(js_file)
                    js_components_found += 1
            except Exception as e:
                print(f"[React + Axe]   ⚠️ Error leyendo {js_file}: {e}")
                continue
        
        print(f"[React + Axe]   → {js_components_found} archivos .js identificados como componentes")
        
        ts_components_found = 0
        for ts_file in ts_files:
            # Saltar archivos de configuración comunes (pero NO index.ts - puede ser componente)
            if any(skip in str(ts_file) for skip in skip_patterns):
                continue
            try:
                content = ts_file.read_text(encoding="utf-8", errors="ignore")
                # Si el archivo es muy pequeño, probablemente no es un componente
                if len(content) < 30:
                    continue
                # Buscar indicadores de componente React (MUY permisivo)
                has_react_import = bool(re.search(r'import\s+.*from\s+["\']react["\']', content, re.IGNORECASE))
                has_jsx = bool(re.search(r'<[a-zA-Z]', content))  # Cualquier JSX
                has_return_jsx = bool(re.search(r'return\s+<', content) or re.search(r'return\s+\(', content))
                has_export = bool(re.search(r'export\s+(default\s+)?(function|const|class)', content))
                
                # Si importa React Y tiene JSX, es muy probable que sea un componente
                if has_react_import and (has_jsx or has_return_jsx):
                    components.append(ts_file)
                    ts_components_found += 1
                # O si exporta algo y tiene JSX
                elif has_export and has_jsx:
                    components.append(ts_file)
                    ts_components_found += 1
            except Exception as e:
                print(f"[React + Axe]   ⚠️ Error leyendo {ts_file}: {e}")
                continue
        
        print(f"[React + Axe]   → {ts_components_found} archivos .ts identificados como componentes")
    
    return components


def map_axe_violations_to_react_components(
    axe_results: Dict, project_root: Path, source_roots: Optional[List[Path]] = None
) -> Dict[str, List[Dict]]:
    """
    Mapea las violaciones de Axe (sobre HTML renderizado) a los componentes React (*.jsx, *.tsx).
    
    Estrategia idéntica a Angular pero adaptada para JSX.
    """
    if not axe_results:
        return {}
    
    violations = axe_results.get("violations", []) or []
    if not violations:
        return {}
    
    # FILTRAR: Solo violaciones WCAG A y AA (critical y serious)
    # WCAG A = critical, WCAG AA = serious
    wcag_violations = [
        v for v in violations 
        if v.get("impact") in ["critical", "serious"]
    ]
    
    if not wcag_violations:
        print(f"[React + Axe] ⚠️ No se encontraron violaciones WCAG A/AA (critical/serious)")
        print(f"[React + Axe] Total violaciones detectadas: {len(violations)}")
        if violations:
            impacts = {}
            for v in violations:
                impact = v.get("impact", "unknown")
                impacts[impact] = impacts.get(impact, 0) + 1
            print(f"[React + Axe] Distribución por impacto: {impacts}")
        return {}
    
    print(f"[React + Axe] Filtrando violaciones WCAG A/AA:")
    print(f"  - Total violaciones detectadas: {len(violations)}")
    print(f"  - Violaciones WCAG A/AA (critical/serious): {len(wcag_violations)}")
    
    # Determinar source_roots si no se pasan
    if source_roots is None:
        possible_roots = [
            project_root / "src",
            project_root / "app",
            project_root / "components",
            project_root / "pages",
            project_root,
        ]
        source_roots = [root for root in possible_roots if root.exists()]
        
        # Fallback: si no se encontró nada, usar src/ aunque no exista (comportamiento original)
        if not source_roots:
            source_roots = [project_root / "src"]
    
    print(f"[React + Axe] Buscando componentes en: {[str(r) for r in source_roots]}")
    
    # Cargar todos los componentes React en memoria
    components: Dict[str, Dict[str, str]] = {}
    all_found_components = []
    for root in source_roots:
        found = discover_react_components([root])
        all_found_components.extend(found)
        print(f"[React + Axe]   → Encontrados {len(found)} componente(s) en {root}")
    
    # Si no se encontraron componentes en los directorios esperados, buscar en TODO el proyecto
    if len(all_found_components) == 0:
        print(f"[React + Axe] ⚠️ No se encontraron componentes en directorios esperados, buscando en todo el proyecto...")
        # Asegurarse de que project_root existe antes de buscar
        if project_root.exists():
            all_found_components = discover_react_components([project_root])
            print(f"[React + Axe]   → Encontrados {len(all_found_components)} componente(s) en todo el proyecto")
        else:
            print(f"[React + Axe] ⚠️ ERROR: El directorio del proyecto no existe: {project_root}")
        
        # Si aún no encuentra nada, mostrar algunos archivos para diagnóstico
        if len(all_found_components) == 0 and project_root.exists():
            print(f"[React + Axe] ⚠️ DIAGNÓSTICO: Listando algunos archivos encontrados para verificar...")
            try:
                js_files = [f for f in project_root.glob("**/*.js") if "node_modules" not in str(f)][:10]
                jsx_files = [f for f in project_root.glob("**/*.jsx") if "node_modules" not in str(f)][:10]
                ts_files = [f for f in project_root.glob("**/*.ts") if "node_modules" not in str(f)][:10]
                tsx_files = [f for f in project_root.glob("**/*.tsx") if "node_modules" not in str(f)][:10]
                print(f"[React + Axe]   → Archivos .js encontrados: {len(js_files)} ejemplos")
                if js_files:
                    print(f"[React + Axe]     Ejemplos: {[str(f.relative_to(project_root)) for f in js_files[:3]]}")
                print(f"[React + Axe]   → Archivos .jsx encontrados: {len(jsx_files)} ejemplos")
                if jsx_files:
                    print(f"[React + Axe]     Ejemplos: {[str(f.relative_to(project_root)) for f in jsx_files[:3]]}")
                print(f"[React + Axe]   → Archivos .ts encontrados: {len(ts_files)} ejemplos")
                print(f"[React + Axe]   → Archivos .tsx encontrados: {len(tsx_files)} ejemplos")
            except Exception as e:
                print(f"[React + Axe]   ⚠️ Error listando archivos: {e}")
                import traceback
                traceback.print_exc()
    
    print(f"[React + Axe] Total componentes encontrados: {len(all_found_components)}")
    
    # Mostrar algunos ejemplos de componentes encontrados
    if all_found_components:
        print(f"[React + Axe] Ejemplos de componentes encontrados:")
        for comp in all_found_components[:5]:  # Mostrar primeros 5
            print(f"  - {comp.relative_to(project_root) if project_root in comp.parents else comp}")
    
    for comp_path in all_found_components:
        try:
            rel_path = comp_path.relative_to(project_root)
            jsx_content = comp_path.read_text(encoding="utf-8")
            normalized = _normalize_react_html(jsx_content)
            components[str(rel_path)] = {
                "jsx": jsx_content,
                "normalized": normalized,
            }
        except Exception as e:
            print(f"[React + Axe] ⚠️ Error cargando {comp_path}: {e}")
            continue
    
    issues_by_component: Dict[str, List[Dict]] = {}
    
    print(f"[React + Axe] Mapeando {len(wcag_violations)} violación(es) WCAG A/AA a componentes...")
    
    for violation in wcag_violations:
        violation_id = violation.get("id", "")
        violation_description = violation.get("description", "")
        impact = violation.get("impact", "unknown")
        wcag_level = "WCAG A" if impact == "critical" else "WCAG AA" if impact == "serious" else "Otro"
        print(f"  → Violación [{wcag_level}]: {violation_id} - {violation_description} (impacto: {impact})")
        
        for node in violation.get("nodes", []):
            html_snippet = node.get("html") or ""
            if not html_snippet:
                continue
            
            normalized_snippet = _normalize_react_html(html_snippet)
            if not normalized_snippet.strip():
                continue
            
            targets = node.get("target", [])
            selector = targets[0] if targets and isinstance(targets[0], str) else ""
            
            matched_component = None
            match_method = ""
            
            # 1) Búsqueda sobre contenido normalizado
            for rel_path, comp_data in components.items():
                if normalized_snippet in comp_data["normalized"]:
                    matched_component = rel_path
                    match_method = "contenido normalizado"
                    break
            
            # 2) Buscar por clases CSS específicas del snippet (más preciso)
            if not matched_component and html_snippet:
                # Extraer todas las clases del snippet HTML
                classes_in_snippet = re.findall(r'class=["\']([^"\']+)["\']', html_snippet)
                if classes_in_snippet:
                    all_classes = ' '.join(classes_in_snippet).split()
                    # Buscar componentes que contengan TODAS las clases principales
                    for rel_path, comp_data in components.items():
                        # Verificar que al menos algunas clases importantes estén presentes
                        matching_classes = [cls for cls in all_classes if cls in comp_data["jsx"]]
                        if len(matching_classes) >= min(2, len(all_classes)):  # Al menos 2 clases o todas si hay menos
                            # Validar que el tag principal también existe
                            snippet_tag = re.search(r'<(\w+)', html_snippet)
                            if snippet_tag:
                                tag_name = snippet_tag.group(1)
                                if f'<{tag_name}' in comp_data["jsx"] or f'<{tag_name} ' in comp_data["jsx"]:
                                    matched_component = rel_path
                                    match_method = f"clases CSS ({', '.join(matching_classes[:3])})"
                                    break
            
            # 3) Fallback: buscar en JSX crudo (solo si no se encontró con clases)
            if not matched_component:
                for rel_path, comp_data in components.items():
                    if _jsx_contains_html_elements(comp_data["jsx"], normalized_snippet):
                        # Validar que el tag principal realmente existe en el componente
                        snippet_tag = re.search(r'<(\w+)', html_snippet)
                        if snippet_tag:
                            tag_name = snippet_tag.group(1)
                            if f'<{tag_name}' in comp_data["jsx"] or f'<{tag_name} ' in comp_data["jsx"]:
                                matched_component = rel_path
                                match_method = "coincidencia de tags"
                                break
            
            # 4) Usar selector CSS para encontrar componentes (mejorado)
            if not matched_component and selector:
                # Extraer nombre de clase sin el punto inicial
                class_name = selector.lstrip('.').split()[0] if selector.startswith('.') else selector.split()[0]
                # Variaciones del nombre de clase
                class_variations = [
                    class_name,
                    class_name.lower(),
                    class_name.capitalize(),
                    class_name.replace('-', '_'),
                    class_name.replace('_', '-'),
                ]
                
                for rel_path, comp_data in components.items():
                    # Buscar el selector completo
                    if selector in comp_data["jsx"] or selector in comp_data["normalized"]:
                        matched_component = rel_path
                        match_method = "selector CSS"
                        break
                    
                    # Buscar variaciones del nombre de clase
                    for variation in class_variations:
                        if variation and (variation in comp_data["jsx"] or variation in comp_data["normalized"]):
                            matched_component = rel_path
                            match_method = f"selector CSS (variación: {variation})"
                            break
                    if matched_component:
                        break
            
            # 5) Buscar por texto visible en el HTML snippet (mejorado - más específico)
            if not matched_component and html_snippet:
                # Extraer texto visible del HTML (sin tags)
                text_content = re.sub(r'<[^>]+>', '', html_snippet).strip()
                # Limpiar espacios múltiples
                text_content = re.sub(r'\s+', ' ', text_content)
                # Buscar texto significativo (más de 3 caracteres)
                if len(text_content) > 3:
                    # Primero intentar búsqueda exacta del texto completo
                    for rel_path, comp_data in components.items():
                        # Buscar el texto completo en el JSX
                        if text_content in comp_data["jsx"]:
                            # Validar que el tag también existe
                            snippet_tag = re.search(r'<(\w+)', html_snippet)
                            if snippet_tag:
                                tag_name = snippet_tag.group(1)
                                if f'<{tag_name}' in comp_data["jsx"] or f'<{tag_name} ' in comp_data["jsx"]:
                                    matched_component = rel_path
                                    match_method = f"texto visible exacto: '{text_content[:30]}...'"
                                    break
                    
                    # Si no se encontró texto exacto, buscar palabras clave significativas
                    if not matched_component:
                        words = [w for w in text_content.split() if len(w) > 3]
                        if words:
                            # Buscar componentes que contengan múltiples palabras clave
                            for rel_path, comp_data in components.items():
                                matching_words = [w for w in words if w in comp_data["jsx"]]
                                if len(matching_words) >= min(2, len(words)):  # Al menos 2 palabras o todas si hay menos
                                    # Validar que el tag también existe
                                    snippet_tag = re.search(r'<(\w+)', html_snippet)
                                    if snippet_tag:
                                        tag_name = snippet_tag.group(1)
                                        if f'<{tag_name}' in comp_data["jsx"] or f'<{tag_name} ' in comp_data["jsx"]:
                                            matched_component = rel_path
                                            match_method = f"texto visible (palabras: {', '.join(matching_words[:3])})"
                                            break
            
            # 6) Estrategias específicas para iframes
            if not matched_component and "iframe" in html_snippet.lower():
                # Buscar en componentes comunes (App.js, index.js)
                common_names = ["App.js", "App.jsx", "App.tsx", "index.js", "index.jsx"]
                for rel_path in components.keys():
                    if any(name in rel_path for name in common_names):
                        matched_component = rel_path
                        match_method = "componente común (iframe)"
                        break
                
                # Si no, buscar por indicadores CSS (position: fixed)
                if not matched_component:
                    for rel_path, comp_data in components.items():
                        if "position" in comp_data["jsx"] and "fixed" in comp_data["jsx"]:
                            matched_component = rel_path
                            match_method = "indicador CSS (iframe)"
                            break
                
                # Último recurso: primer componente disponible
                if not matched_component and components:
                    matched_component = list(components.keys())[0]
                    match_method = "fallback (iframe)"
            
            # NO usar fallback genérico - si no se encuentra, no mapear
            # Esto evita mapear violaciones a componentes incorrectos
            
            if matched_component:
                if matched_component not in issues_by_component:
                    issues_by_component[matched_component] = []
                
                issues_by_component[matched_component].append({
                    "violation": violation,
                    "node": node,
                })
                if "fallback" in match_method:
                    print(f"    ⚠️ Mapeado con fallback a {matched_component} (método: {match_method})")
                    print(f"      Nota: No se encontró coincidencia exacta, usando componente por defecto")
                else:
                    print(f"    ✓ Mapeado a {matched_component} (método: {match_method})")
            else:
                # Mostrar más información de debug
                html_preview = html_snippet[:100].replace('\n', ' ') if html_snippet else "N/A"
                print(f"    ⚠️ No se pudo mapear (selector: {selector[:50] if selector else 'N/A'}...)")
                print(f"      HTML snippet: {html_preview}...")
                if selector:
                    class_name = selector.lstrip('.').split()[0] if selector.startswith('.') else ""
                    if class_name:
                        print(f"      Intentó buscar clase: {class_name}")
                print(f"      Total componentes disponibles: {len(components)}")
    
    # Filtrar componentes que estén en node_modules (no queremos tocar librerías de terceros)
    original_count = len(issues_by_component)
    filtered_issues_by_component: Dict[str, List[Dict]] = {
        rel_path: issues
        for rel_path, issues in issues_by_component.items()
        if "node_modules" not in rel_path.replace("/", "\\")
    }

    if original_count > 0 and not filtered_issues_by_component:
        print("[React + Axe] ⚠️ Todas las violaciones mapeadas pertenecen a archivos en node_modules.")
        print("  → No se aplicarán correcciones en código de terceros (librerías).")
        print("  → Si quieres corregir esos errores, copia el markup a tus propios componentes en src/.")
        return {}

    issues_by_component = filtered_issues_by_component

    print(f"[React + Axe] Total de componentes con violaciones mapeadas: {len(issues_by_component)}")
    for rel_path, issues in issues_by_component.items():
        print(f"  - {rel_path}: {len(issues)} violación(es)")
    
    print(f"[React + Axe] ✓ Se han asociado violaciones de Axe a {len(issues_by_component)} componente(s).")
    
    return issues_by_component


def _build_axe_based_prompt_for_react_component(
    component_path: str, component_content: str, issues: List[Dict]
) -> str:
    """
    Prompt compacto para corregir accesibilidad en un componente React
    a partir de violaciones de Axe.
    """
    violation_lines: List[str] = []

    for issue in issues:
        if not isinstance(issue, dict):
            continue

        violation = issue.get("violation", {}) or {}
        node = issue.get("node", {}) or {}

        v_id = violation.get("id", "unknown")
        impact = violation.get("impact", "moderate")
        desc = violation.get("description", "")
        html_snippet = (node.get("html") or "").strip()

        # Tag principal del snippet
        tag = "elemento"
        m = re.search(r"<(\w+)", html_snippet)
        if m:
            tag = m.group(1)

        line = f"- {v_id} ({impact}) en <{tag}>"
        if desc:
            line += f": {desc}"

        violation_lines.append(line)

        if html_snippet:
            first_line = html_snippet.splitlines()[0].strip()
            violation_lines.append(f"  HTML: {first_line[:200]}...")

    violations_text = "\n".join(violation_lines)
    total = len(issues)

    # Detectar si hay errores de contraste para dar instrucciones más específicas
    has_contrast = any(issue.get("violation", {}).get("id", "") == "color-contrast" for issue in issues)
    
    contrast_instructions = ""
    if has_contrast:
        contrast_instructions = """
🚨 CRÍTICO - CORRECCIÓN DE CONTRASTE:
Estos son errores REALES detectados por Axe en la aplicación renderizada. DEBES corregirlos TODOS.

Para corregir errores de contraste:
1. LOCALIZA el elemento usando el fragmento HTML proporcionado en "HTML: ..."
   - Busca el elemento EXACTO en el código JSX que coincida con ese HTML
   - Busca por:
     * El texto contenido (ej: "Code", "Chat on whatsapp", "Save Contact")
     * Las clases CSS específicas (ej: "btn-outline-light", "btn-success", "btn-outline-dark mx-1")
     * La estructura del elemento (tag + clases + texto)
   - Ignora atributos dinámicos de React (data-react-*, className generados, etc.)
   - Si NO encuentras el elemento en este componente, busca en otros componentes del proyecto:
     * Busca archivos que contengan el texto o las clases del HTML snippet
     * Los elementos pueden estar en App.js, Home.js, Header.js, Footer.js, u otros componentes
   - ⚠️ IMPORTANTE: Si el elemento NO está en este componente, DEBES indicarlo claramente o buscar en otros archivos

2. CORRIGE el color del texto según el fondo:
   - Si el fondo es CLARO (blanco, gris claro, colores claros): usa texto OSCURO
     * style={{ color: '#000000' }} o color="#000000" o color="black"
   - Si el fondo es OSCURO (negro, gris oscuro, colores oscuros): usa texto CLARO
     * style={{ color: '#FFFFFF' }} o color="#FFFFFF" o color="white"

3. FORMATOS VÁLIDOS en React/JSX:
   - style={{ color: '#000000' }} (estilo inline)
   - color="#000000" (prop de Chakra UI como <Text color="#000000">)
   - color="black" (prop de Chakra UI con nombre de color)
   - Si el elemento ya tiene style={{ ... }}, añade color dentro del mismo objeto

4. IMPORTANTE:
   - Si el elemento usa Chakra UI (Text, Heading, Button, etc.), puedes modificar la prop color="..."
   - Si el elemento es HTML nativo (<span>, <p>, <button>, etc.), usa style={{ color: '...' }}
   - NO cambies colores de fondo, solo el color del texto
   - NO devuelvas el código sin cambios si hay violaciones de contraste listadas

⚠️ NO devuelvas el mismo código. DEBES hacer cambios reales en los colores."""
    
    prompt = f"""Corrige TODAS las {total} violaciones WCAG A/AA en este componente React.

COMPONENTE: {component_path}

VIOLACIONES:
{violations_text}
{contrast_instructions}

REGLAS RÁPIDAS:
- color-contrast → ajusta SOLO el color del texto (style={{ color: '...' }} o color="...") según el fondo
- aria-input-field-name / label → <label htmlFor="id"> o aria-label="texto" en inputs/selects
- button-name → texto visible o aria-label="acción" en <button>
- link-name → texto descriptivo o aria-label="destino" en <a>
- image-alt / role-img-alt → alt="..." o aria-label="..." en imágenes/roles visuales
- frame-title → title="..." en <iframe>
- select-name → <label htmlFor> o aria-label en <select>
- target-size → padding / minWidth / minHeight para área táctil (~44x44px)
- nested-interactive → evita <button> dentro de <a> (y viceversa)

INSTRUCCIONES:
- Corrige SOLO los elementos indicados en la lista de violaciones.
- LOCALIZACIÓN PRECISA: Para cada violación, busca el elemento EXACTO usando:
  * El texto visible del HTML snippet (ej: "Code", "Chat on whatsapp", "Save Contact")
  * Las clases CSS del snippet (ej: "btn-outline-light", "btn-success", "btn-outline-dark mx-1 d-flex")
  * El tag y estructura del elemento
- Si NO encuentras el elemento en este componente:
  * El elemento puede estar en otro componente (App.js, Home.js, Header.js, Footer.js, etc.)
  * Busca en el proyecto por el texto o clases del HTML snippet
  * Si no puedes acceder a otros componentes, indica claramente que el elemento no está en este archivo
- Mantén hooks, props, estado y lógica React sin cambios.
- No cambies layout (width, height, margin, padding, display, position, flex, grid).
- No elimines ni añadas componentes JSX grandes; añade/modifica atributos en elementos existentes.
- ⚠️ CRÍTICO: Si hay violaciones de contraste listadas, DEBES cambiar los colores. NO devuelvas el código sin cambios.
- ⚠️ CRÍTICO: Si el elemento NO está en este componente, NO lo inventes. Busca en otros archivos o indica que no se encontró.

COMPONENTE COMPLETO (ACTUAL):
```jsx
{component_content}
```

Devuelve SOLO el componente completo corregido, sin explicaciones."""

    return prompt.strip()


def _get_specific_instruction_for_violation(violation_id: str, html_snippet: str, contrast_info: str) -> str:
    """Devuelve una instrucción específica y concisa para cada tipo de violación."""
    v_lower = violation_id.lower()
    
    if "color-contrast" in v_lower:
        if contrast_info:
            # Extraer datos de contraste de forma simple
            bg = "#ffffff"  # default
            if "Color de fondo:" in contrast_info:
                try:
                    bg = contrast_info.split("Color de fondo:")[1].split("\n")[0].strip()
                except:
                    pass
            recommended = "#000000" if any(c in bg.lower() for c in ["#ff", "#fff", "#00d1", "white", "light"]) else "#FFFFFF"
            return f"Añade style={{'color': '{recommended}'}} al elemento (fondo: {bg})"
        return "Añade style={{'color': '#000000'}} o style={{'color': '#FFFFFF'}} según el fondo"
    
    if "aria-input-field-name" in v_lower or "label" in v_lower or "form-field" in v_lower:
        return "Añade <label htmlFor=\"id\"> o aria-label=\"texto descriptivo\" al input/select/textarea"
    
    if "button-name" in v_lower:
        return "Añade texto visible dentro del <button> o aria-label=\"acción\" si solo tiene iconos"
    
    if "link-name" in v_lower:
        return "Añade texto descriptivo dentro del <a> o aria-label=\"destino\" si solo tiene iconos"
    
    if "image-alt" in v_lower or "img" in v_lower:
        return "Añade alt=\"descripción\" o alt=\"\" si la imagen es decorativa"
    
    if "frame-title" in v_lower:
        return "Añade title=\"descripción del contenido\" al <iframe>"
    
    if "select-name" in v_lower:
        return "Añade <label htmlFor=\"id\"> o aria-label=\"texto\" al <select>"
    
    if "target-size" in v_lower:
        return "Aumenta el área táctil (min 44x44px) con padding o minWidth/minHeight en style"
    
    if "nested-interactive" in v_lower:
        return "Separa elementos interactivos: no <button> dentro de <a>, no <a> dentro de <button>"
    
    if "aria-allowed-attr" in v_lower:
        return "Elimina atributos ARIA no permitidos para el role del elemento"
    
    if "aria-required-children" in v_lower:
        return "Añade los elementos hijos requeridos para el role o cambia el role a uno válido"
    
    if "aria-valid-attr-value" in v_lower:
        return "Corrige valores inválidos de atributos ARIA (ej: role=\"invalid\" → role=\"button\")"
    
    if "aria-toggle" in v_lower:
        return "Añade aria-label=\"estado del toggle\" al elemento con role=\"switch\" o role=\"checkbox\""
    
    return "Lee la descripción y aplica la corrección mínima necesaria"


def fix_react_components_with_axe_violations(
    issues_by_component: Dict[str, List[Dict]], project_root: Path, client, screenshot_paths: Optional[List[str]] = None
) -> Dict[str, Dict[str, str]]:
    """
    Usa la información de Axe para pedir al LLM que corrija los componentes React.
    
    Esta función es IDÉNTICA a fix_templates_with_axe_violations de Angular pero para React.
    """
    fixes: Dict[str, Dict[str, str]] = {}
    
    if not issues_by_component:
        print("[React + Axe] No hay violaciones mapeadas a componentes.")
        return fixes
    
    for rel_path, issues in issues_by_component.items():
        try:
            comp_path = project_root / rel_path
            if not comp_path.exists():
                continue
            
            original_content = comp_path.read_text(encoding="utf-8")
            
            if not original_content.strip():
                continue
            
            prompt = _build_axe_based_prompt_for_react_component(rel_path, original_content, issues)
            
            system_message = (
                "Eres un EXPERTO en accesibilidad web (WCAG 2.2 A+AA) y React. "
                "Tu MISIÓN es corregir TODAS las violaciones de accesibilidad indicadas por Axe "
                "modificando el componente JSX completo. "
                "🚨 CRÍTICO: DEBES hacer cambios reales al código. NO devuelvas el mismo código. "
                "🚨 Si hay violaciones de contraste, DEBES añadir o modificar style={{ color: '...' }} o color=\"...\" "
                "🚨 Si hay violaciones de aria-label, button-name, link-name, etc., DEBES añadir los atributos necesarios. "
                "🚨 Mantén la lógica React (hooks, props, estado) sin romperla. "
                "🚨 NO modifiques el diseño responsive - las correcciones deben ser invisibles visualmente. "
                "🚨 Para contraste de color, SOLO ajusta el color del texto, NO cambies layout ni fondos. "
                "🚨 Si devuelves el mismo código sin cambios, la corrección FALLA completamente. "
                "⚠️ IMPORTANTE: Si hay errores de contraste listados, DEBES cambiar los colores. "
                "⚠️ Si el código ya tiene un color pero Axe reporta error, significa que: "
                "   a) El color no se está aplicando correctamente (añade !important o usa style inline), O "
                "   b) Estás cambiando el elemento incorrecto. "
                "⚠️ Busca el elemento EXACTO usando el 'Fragmento HTML afectado' y asegúrate de cambiar el color correcto. "
                "⚠️ NO devuelvas el código sin cambios si hay violaciones de contraste reportadas."
            )
            
            print(f"[React + Axe] Corrigiendo componente basado en Axe: {rel_path}")
            print(f"[React + Axe] Violaciones a corregir: {len(issues)}")
            for i, issue in enumerate(issues, 1):
                violation_id = issue.get("violation", {}).get("id", "unknown")
                print(f"  {i}. {violation_id}")
            
            # Log del prompt para debugging
            print(f"[React + Axe] 📝 Prompt generado (primeros 1500 chars):")
            print(prompt[:1500])
            print(f"[React + Axe] ... (total: {len(prompt)} chars)")
            
            # Log del código actual para comparar
            print(f"[React + Axe] 📄 Código actual (primeros 500 chars):")
            print(original_content[:500])
            
            messages = [
                {"role": "system", "content": system_message},
            ]
            
            has_contrast_errors = any(
                issue.get("violation", {}).get("id", "") == "color-contrast"
                for issue in issues
            )
            
            if screenshot_paths and has_contrast_errors:
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
   - Los colores de fondo REALES que se ven en las capturas

2. CORRIGE TODOS LOS ERRORES de contraste listados arriba, PERO:
   - MANTÉN el diseño visual que ves en las capturas
   - NO cambies colores de fondo, tamaños de elementos, o distribución que se vea en las imágenes
   - Para errores de contraste: ajusta SOLO el color del texto basándote en el fondo REAL que ves en las capturas
   - Si el fondo es CLARO en las capturas: usa texto OSCURO (#000000, #212121)
   - Si el fondo es OSCURO en las capturas: usa texto CLARO (#FFFFFF, #F5F5F5)
   - NO añadas elementos visibles nuevos (usa aria-label o sr-only en su lugar)
   - NO cambies display:none a display:block si en las capturas no se ve ese elemento
   - Respeta el diseño responsive: si en mobile se ve de una forma, mantén esa forma

3. TU OBJETIVO: Corregir TODOS los errores de contraste SIN cambiar cómo se ve la página en las capturas.
   - Las correcciones deben ser "invisibles" visualmente
   - Usa ajustes de contraste mínimos basados en los fondos REALES que ves en las capturas
   - El diseño final debe verse IDÉNTICO a las capturas, pero accesible

Las capturas muestran la aplicación ANTES de las correcciones. Tu trabajo es hacerla accesible manteniendo exactamente ese aspecto visual.

🚨 CRÍTICO - NO ROMPAS EL RESPONSIVE:
- NO cambies propiedades de layout en style: width, height, margin, padding, display, position, flex, grid
- NO modifiques className que afecten al responsive
- Para contraste: SOLO cambia color del texto, NO toques layout ni fondos
- El diseño debe verse IDÉNTICO en mobile, tablet y desktop después de las correcciones
"""
                user_content = [
                    {"type": "text", "text": prompt + screenshot_instructions}
                ]
                for screenshot_path in screenshot_paths:
                    try:
                        screenshot_file = Path(screenshot_path)
                        if screenshot_file.exists():
                            with open(screenshot_file, "rb") as img_file:
                                image_base64 = base64.b64encode(img_file.read()).decode('utf-8')
                                mime_type = "image/png"
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
                messages.append({"role": "user", "content": prompt})
            
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.0,
            )
            
            corrected = response.choices[0].message.content or ""
            log_openai_call(
                prompt=prompt,
                response=corrected,
                model="gpt-4o",
                call_type="react_axe_component_fix",
            )
            
            
            corrected = corrected.strip()
            if corrected.startswith("```"):
                parts = corrected.split("```")
                if len(parts) >= 3:
                    code_block = parts[1]
                    if "\n" in code_block:
                        code_block = code_block.split("\n", 1)[1]
                    corrected = code_block.strip()
                else:
                    corrected = corrected.replace("```jsx", "").replace("```tsx", "").replace("```js", "").replace("```", "").strip()

            
            corrected = _apply_react_accessibility_fixes(corrected)
            
            
            corrected = _fix_basic_jsx_syntax_errors(corrected)
            
            # Corregir sintaxis React para atributos ARIA (similar a Angular)
            corrected = _fix_react_aria_syntax(corrected)

            # VALIDACIÓN CRÍTICA: Verificar que el LLM devolvió código válido (IGUAL QUE ANGULAR)
            is_valid_response = True
            
            if corrected.strip().startswith("//") or corrected.strip().startswith("/*"):
                print(f"[React + Axe] ⚠️ El LLM devolvió un comentario en lugar de código para {rel_path}")
                is_valid_response = False
            
            if is_valid_response and not re.search(r'<\w+|import\s+|export\s+|function\s+|const\s+|class\s+', corrected):
                print(f"[React + Axe] ⚠️ El LLM no devolvió código React/JSX válido para {rel_path}")
                is_valid_response = False
            
            if is_valid_response and len(corrected.strip()) < len(original_content.strip()) * 0.5:
                print(f"[React + Axe] ⚠️ La respuesta del LLM es demasiado corta para {rel_path} ({len(corrected)} vs {len(original_content)} chars)")
                is_valid_response = False

            # VALIDACIÓN: Verificar que no se añadieron elementos nuevos
            orig_tags = set(re.findall(r'<(\w+)', original_content))
            corr_tags = set(re.findall(r'<(\w+)', corrected)) if corrected else set()
            new_tags = corr_tags - orig_tags
            
            # Tags permitidos que pueden añadirse (solo <label> para inputs sin label)
            allowed_new_tags = {'label'}
            problematic_new_tags = new_tags - allowed_new_tags
            
            if problematic_new_tags:
                print(f"[React + Axe] ⚠️ El LLM añadió elementos nuevos no permitidos: {problematic_new_tags}")
                print(f"[React + Axe] ⚠️ NO se aplicarán los cambios para evitar añadir errores")
                is_valid_response = False
            
            # COMPARAR Y APLICAR (MEJORADO - Similar a Angular pero para React/JSX)
            # Detectar diferencias más robustamente (incluyendo cambios de color en diferentes formatos)
            
            # 1. Detectar colores en style={{ color: '...' }}
            orig_colors_style = re.findall(r'style\s*=\s*\{\s*[^}]*color\s*:\s*["\']?([^"\';}]+)', original_content, re.IGNORECASE)
            corr_colors_style = re.findall(r'style\s*=\s*\{\s*[^}]*color\s*:\s*["\']?([^"\';}]+)', corrected, re.IGNORECASE) if corrected else []
            
            # 2. Detectar colores en propiedades de Chakra UI (color="black", color={'black'})
            orig_colors_prop = re.findall(r'color\s*=\s*["\']([^"\']+)["\']', original_content, re.IGNORECASE)
            corr_colors_prop = re.findall(r'color\s*=\s*["\']([^"\']+)["\']', corrected, re.IGNORECASE) if corrected else []
            
            # 3. Detectar colores en formato CSS tradicional (color: '...')
            orig_colors_css = re.findall(r'color\s*:\s*["\']?([^"\';]+)', original_content, re.IGNORECASE)
            corr_colors_css = re.findall(r'color\s*:\s*["\']?([^"\';]+)', corrected, re.IGNORECASE) if corrected else []
            
            # Combinar todos los colores encontrados
            orig_colors = set(orig_colors_style + orig_colors_prop + orig_colors_css)
            corr_colors = set(corr_colors_style + corr_colors_prop + corr_colors_css)
            has_color_diff = orig_colors != corr_colors
            
            # Comparación más robusta: normalizar espacios pero detectar cambios reales
            orig_normalized = re.sub(r'\s+', ' ', original_content.strip())
            corr_normalized = re.sub(r'\s+', ' ', corrected.strip()) if corrected else ""
            
            # Detectar cambios en atributos ARIA, alt, aria-label, etc.
            orig_aria = set(re.findall(r'aria-\w+=["\'][^"\']*["\']', original_content, re.IGNORECASE))
            corr_aria = set(re.findall(r'aria-\w+=["\'][^"\']*["\']', corrected, re.IGNORECASE)) if corrected else set()
            has_aria_diff = orig_aria != corr_aria
            
            orig_alt = set(re.findall(r'alt=["\'][^"\']*["\']', original_content, re.IGNORECASE))
            corr_alt = set(re.findall(r'alt=["\'][^"\']*["\']', corrected, re.IGNORECASE)) if corrected else set()
            has_alt_diff = orig_alt != corr_alt
            
            orig_labels = set(re.findall(r'<label[^>]*>', original_content, re.IGNORECASE))
            corr_labels = set(re.findall(r'<label[^>]*>', corrected, re.IGNORECASE)) if corrected else set()
            has_label_diff = orig_labels != corr_labels
            
            # Detectar cambios en style={{ ... }} completo (puede incluir color u otros estilos)
            orig_styles = set(re.findall(r'style\s*=\s*\{\s*\{[^}]+\}\s*\}', original_content, re.IGNORECASE))
            corr_styles = set(re.findall(r'style\s*=\s*\{\s*\{[^}]+\}\s*\}', corrected, re.IGNORECASE)) if corrected else set()
            has_style_diff = orig_styles != corr_styles
            
            has_changes = (
                orig_normalized != corr_normalized or
                has_color_diff or
                has_aria_diff or
                has_alt_diff or
                has_label_diff or
                has_style_diff
            )
            
            if is_valid_response and corrected and has_changes:
                if has_color_diff:
                    print(f"[React + Axe] 🎨 Diferencia en colores detectada: {sorted(orig_colors)} -> {sorted(corr_colors)}")
                if has_aria_diff:
                    print(f"[React + Axe] 🎨 Diferencia en ARIA detectada: {len(orig_aria)} -> {len(corr_aria)} atributos")
                if has_alt_diff:
                    print(f"[React + Axe] 🎨 Diferencia en alt detectada: {len(orig_alt)} -> {len(corr_alt)} atributos")
                comp_path.write_text(corrected, encoding="utf-8")
                fixes[rel_path] = {
                    "original": original_content,
                    "corrected": corrected,
                }
                print(f"[React + Axe] ✓ Cambios aplicados en {rel_path}")
            else:
                if not is_valid_response:
                    print(f"[React + Axe] ⚠️ El LLM devolvió código inválido para {rel_path}")
                else:
                    print(f"[React + Axe] ⚠️ El LLM devolvió el mismo código para {rel_path}")
                    # Si hay violaciones de contraste pero no se detectaron cambios, mostrar más info
                    has_contrast = any(issue.get("violation", {}).get("id", "") == "color-contrast" for issue in issues)
                    if has_contrast:
                        print(f"[React + Axe] ⚠️ HAY VIOLACIONES DE CONTRASTE PERO NO SE DETECTARON CAMBIOS")
                        print(f"[React + Axe] Colores en original: {sorted(orig_colors)}")
                        print(f"[React + Axe] Colores en corregido: {sorted(corr_colors)}")
                        print(f"[React + Axe] Estilos en original: {len(orig_styles)}")
                        print(f"[React + Axe] Estilos en corregido: {len(corr_styles)}")
                        print(f"[React + Axe] El LLM probablemente no aplicó las correcciones")
                        print("[React + Axe] 💡 Sugerencia: Verifica que el LLM añadió style={{ color: '...' }} "
                              "o modificó la prop color=\"...\")")

        except Exception as e:
            print(f"[React + Axe] ⚠️ Error corrigiendo {rel_path}: {e}")
    
    return fixes


def _apply_react_accessibility_fixes(jsx_content: Optional[str]) -> Optional[str]:
    """Aplica correcciones automáticas de accesibilidad a JSX (igual que Angular)."""
    if not jsx_content:
        return jsx_content
    
    corrected = jsx_content
    
    i_tags = re.finditer(r'<i\s+[^>]*aria-label=["\'][^"\']*["\'][^>]*>', corrected)
    for match in list(i_tags):
        tag = match.group(0)
        if 'role=' not in tag and 'role={' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)
    
    icon_tags = re.finditer(r'<Icon\s+[^>]*aria-label=["\'][^"\']*["\'][^>]*>', corrected)
    for match in list(icon_tags):
        tag = match.group(0)
        if 'role=' not in tag and 'role={' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)
    
    return corrected


def _fix_basic_jsx_syntax_errors(jsx_content: Optional[str]) -> Optional[str]:
    """Corrige errores básicos de sintaxis JSX comunes (igual que Angular pero para JSX)."""
    if not jsx_content:
        return jsx_content
    
    corrected = jsx_content
    

    corrected = re.sub(
        r'style=\{\s*color:\s*([\'"])([^\'"]+)\1\s*\}',
        r"style={{ color: \1\2\1 }}",
        corrected
    )
    
    corrected = re.sub(
        r'style=\{\s*color:\s*([\'"])([^\'"]+)\1\1\s*\}',
        r"style={{ color: \1\2\1 }}",
        corrected
    )
    
    return corrected


def _fix_react_aria_syntax(jsx_content: Optional[str]) -> Optional[str]:
    """Corrige la sintaxis de atributos ARIA en JSX."""
    if not jsx_content:
        return jsx_content
    return jsx_content


def run_axe_on_react_app(base_url: str, run_path: str, suffix: str = "", take_screenshots_flag: bool = False) -> Tuple[Dict, List[str]]:
    """
    Ejecuta Axe sobre una aplicación React ya levantada y devuelve los resultados.
    """
    driver = None
    screenshot_paths = []
    
    try:
        driver = setup_driver()
        driver.get(base_url)
        
        if take_screenshots_flag:
            # Convertir run_path a Path si es string
            run_path_obj = Path(run_path) if isinstance(run_path, str) else run_path
            # take_screenshots espera: driver, url, output_dir, prefix
            screenshot_paths = take_screenshots(driver, base_url, run_path_obj, prefix=f"screenshot{suffix}" if suffix else "screenshot")
        
        axe_results = run_axe_analysis(driver, base_url)
        
        return axe_results, screenshot_paths
    finally:
        if driver:
            driver.quit()


def process_react_project(project_path: str, client, run_path: str, serve_app: bool = False) -> List[str]:
    """
    Procesa un proyecto React local (flujo clásico sin Axe).
    NOTA: El flujo con Axe se ejecuta en main.py con --react-axe.
    """
    # Este flujo clásico no usa Axe, solo análisis estático si se implementa
    # El flujo con Axe está en main.py (_process_react_project_flow)
    return []
