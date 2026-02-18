"""
React accessibility workflows and Axe‑driven component corrections.

This module encapsulates logic specific to React projects:
    - Detecting React usage from package.json and source files.
    - Running Axe against a React dev server.
    - Mapping violations back to JSX/TSX components and guiding LLM fixes.

All prompts and high‑level behaviour must remain unchanged; refactors here
aim at clearer structure, type hints and documentation only.
"""

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
    Normalise React‑generated HTML so it can be compared with JSX components.

    - Removes runtime‑specific attributes (data-react-*, etc.).
    - Collapses whitespace for more robust comparisons.
    """
    if not html:
        return ""
    
    text = html
    # Strip React runtime "noise" attributes from rendered DOM
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
    
    # Ensure all main tags are present in the JSX
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
            
            # Also check for JSX/TSX files
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
    
    Find .jsx, .tsx, and .js/.ts files that contain JSX
    """
    components: List[Path] = []
    
    for root in source_roots:
        if not root.exists():
            print(f"[React + Axe] ⚠️ Directorio no existe: {root}")
            continue
        # No escanear node_modules
        if "node_modules" in str(root):
            continue
        
        # Find explicit JSX/TSX files (ALWAYS include these)
        jsx_files = [p for p in root.glob("**/*.jsx") if "node_modules" not in str(p)]
        tsx_files = [p for p in root.glob("**/*.tsx") if "node_modules" not in str(p)]
        components.extend(jsx_files)
        components.extend(tsx_files)
        print(f"[React + Axe]   → Encontrados {len(jsx_files)} .jsx y {len(tsx_files)} .tsx")
        
        # ALWAYS also search .js/.ts that may contain JSX
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
            # Skip common config files (but NOT index.js - may be a component)
            if any(skip in str(js_file) for skip in skip_patterns):
                continue
            try:
                content = js_file.read_text(encoding="utf-8", errors="ignore")
                # If file is very small, likely not a component
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
            # Skip common config files (but NOT index.ts - may be a component)
            if any(skip in str(ts_file) for skip in skip_patterns):
                continue
            try:
                content = ts_file.read_text(encoding="utf-8", errors="ignore")
                # If file is very small, likely not a component
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
    
    Same strategy as Angular but adapted for JSX.
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
            print(f"[React + Axe] Distribution by impact: {impacts}")
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
        
        # Fallback: if nothing found, use src/ even if it doesn't exist (original behaviour)
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
        
        # If still nothing, show some files for diagnosis
        if len(all_found_components) == 0 and project_root.exists():
            print(f"[React + Axe] ⚠️ DIAGNOSTIC: Listing some found files to verify...")
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
    
    print(f"[React + Axe] Mapping {len(wcag_violations)} WCAG A/AA violation(s) to components...")
    
    for violation in wcag_violations:
        violation_id = violation.get("id", "")
        violation_description = violation.get("description", "")
        impact = violation.get("impact", "unknown")
        wcag_level = "WCAG A" if impact == "critical" else "WCAG AA" if impact == "serious" else "Otro"
        print(f"  → Violation [{wcag_level}]: {violation_id} - {violation_description} (impact: {impact})")
        
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
            
            # 1) Search on normalised content
            for rel_path, comp_data in components.items():
                if normalized_snippet in comp_data["normalized"]:
                    matched_component = rel_path
                    match_method = "contenido normalizado"
                    break
            
            # 2) Search by snippet's specific CSS classes (more precise)
            if not matched_component and html_snippet:
                # Extraer todas las clases del snippet HTML
                classes_in_snippet = re.findall(r'class=["\']([^"\']+)["\']', html_snippet)
                if classes_in_snippet:
                    all_classes = ' '.join(classes_in_snippet).split()
                    # Buscar componentes que contengan TODAS las clases principales
                    for rel_path, comp_data in components.items():
                        # Ensure at least some important classes are present
                        matching_classes = [cls for cls in all_classes if cls in comp_data["jsx"]]
                        if len(matching_classes) >= min(2, len(all_classes)):  # Al menos 2 clases o todas si hay menos
                            # Ensure main tag also exists
                            snippet_tag = re.search(r'<(\w+)', html_snippet)
                            if snippet_tag:
                                tag_name = snippet_tag.group(1)
                                if f'<{tag_name}' in comp_data["jsx"] or f'<{tag_name} ' in comp_data["jsx"]:
                                    matched_component = rel_path
                                    match_method = f"clases CSS ({', '.join(matching_classes[:3])})"
                                    break
            
            # 3) Fallback: search raw JSX (only if not found via classes)
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
                            match_method = f"CSS selector (variation: {variation})"
                            break
                    if matched_component:
                        break
            
            # 5) Search by visible text in HTML snippet (improved - more specific)
            if not matched_component and html_snippet:
                # Extraer texto visible del HTML (sin tags)
                text_content = re.sub(r'<[^>]+>', '', html_snippet).strip()
                # Collapse multiple spaces
                text_content = re.sub(r'\s+', ' ', text_content)
                # Look for significant text (more than 3 chars)
                if len(text_content) > 3:
                    # First try exact match of full text
                    for rel_path, comp_data in components.items():
                        # Buscar el texto completo en el JSX
                        if text_content in comp_data["jsx"]:
                            # Ensure the tag also exists
                            snippet_tag = re.search(r'<(\w+)', html_snippet)
                            if snippet_tag:
                                tag_name = snippet_tag.group(1)
                                if f'<{tag_name}' in comp_data["jsx"] or f'<{tag_name} ' in comp_data["jsx"]:
                                    matched_component = rel_path
                                    match_method = f"texto visible exacto: '{text_content[:30]}...'"
                                    break
                    
                    # If no exact text match, search for significant keywords
                    if not matched_component:
                        words = [w for w in text_content.split() if len(w) > 3]
                        if words:
                            # Find components that contain multiple keywords
                            for rel_path, comp_data in components.items():
                                matching_words = [w for w in words if w in comp_data["jsx"]]
                                if len(matching_words) >= min(2, len(words)):  # Al menos 2 palabras o todas si hay menos
                                    # Ensure the tag also exists
                                    snippet_tag = re.search(r'<(\w+)', html_snippet)
                                    if snippet_tag:
                                        tag_name = snippet_tag.group(1)
                                        if f'<{tag_name}' in comp_data["jsx"] or f'<{tag_name} ' in comp_data["jsx"]:
                                            matched_component = rel_path
                                            match_method = f"texto visible (palabras: {', '.join(matching_words[:3])})"
                                            break
            
            # 6) Iframe-specific strategies
            if not matched_component and "iframe" in html_snippet.lower():
                # Buscar en componentes comunes (App.js, index.js)
                common_names = ["App.js", "App.jsx", "App.tsx", "index.js", "index.jsx"]
                for rel_path in components.keys():
                    if any(name in rel_path for name in common_names):
                        matched_component = rel_path
                        match_method = "common component (iframe)"
                        break
                
                # Si no, buscar por indicadores CSS (position: fixed)
                if not matched_component:
                    for rel_path, comp_data in components.items():
                        if "position" in comp_data["jsx"] and "fixed" in comp_data["jsx"]:
                            matched_component = rel_path
                            match_method = "indicador CSS (iframe)"
                            break
                
                # Last resort: first available component
                if not matched_component and components:
                    matched_component = list(components.keys())[0]
                    match_method = "fallback (iframe)"
            
            # Do NOT use generic fallback - if not found, do not map
            # Esto evita mapear violaciones a componentes incorrectos
            
            if matched_component:
                if matched_component not in issues_by_component:
                    issues_by_component[matched_component] = []
                
                issues_by_component[matched_component].append({
                    "violation": violation,
                    "node": node,
                })
                if "fallback" in match_method:
                    print(f"    ⚠️ Mapped with fallback to {matched_component} (method: {match_method})")
                    print(f"      Note: No exact match found, using default component")
                else:
                    print(f"    ✓ Mapped to {matched_component} (method: {match_method})")
            else:
                # Show more debug info
                html_preview = html_snippet[:100].replace('\n', ' ') if html_snippet else "N/A"
                print(f"    ⚠️ No se pudo mapear (selector: {selector[:50] if selector else 'N/A'}...)")
                print(f"      HTML snippet: {html_preview}...")
                if selector:
                    class_name = selector.lstrip('.').split()[0] if selector.startswith('.') else ""
                    if class_name:
                        print(f"      Tried to find class: {class_name}")
                print(f"      Total componentes disponibles: {len(components)}")
    
    # Filter out components in node_modules (we don't want to touch third-party libs)
    original_count = len(issues_by_component)
    filtered_issues_by_component: Dict[str, List[Dict]] = {
        rel_path: issues
        for rel_path, issues in issues_by_component.items()
        if "node_modules" not in rel_path.replace("/", "\\")
    }

    if original_count > 0 and not filtered_issues_by_component:
        print("[React + Axe] ⚠️ Todas las violaciones mapeadas pertenecen a archivos en node_modules.")
        print("  → No fixes will be applied to third-party code (libraries).")
        print("  → Si quieres corregir esos errores, copia el markup a tus propios componentes en src/.")
        return {}

    issues_by_component = filtered_issues_by_component

    print(f"[React + Axe] Total de componentes con violaciones mapeadas: {len(issues_by_component)}")
    for rel_path, issues in issues_by_component.items():
        print(f"  - {rel_path}: {len(issues)} violation(s)")
    
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

    # Detect contrast errors to give more specific instructions
    has_contrast = any(issue.get("violation", {}).get("id", "") == "color-contrast" for issue in issues)
    
    contrast_instructions = ""
    if has_contrast:
        contrast_instructions = """
🚨 CRITICAL - CONTRAST FIX:
These are REAL errors detected by Axe on the rendered application. You MUST fix ALL of them.

To fix contrast errors:
1. LOCATE the element using the HTML fragment provided in "HTML: ..."
   - Find the EXACT element in the JSX code that matches that HTML
   - Search by:
     * Contained text (e.g. "Code", "Chat on whatsapp", "Save Contact")
     * Specific CSS classes (e.g. "btn-outline-light", "btn-success", "btn-outline-dark mx-1")
     * Element structure (tag + classes + text)
   - Ignore dynamic React attributes (data-react-*, generated className, etc.)
   - If you do NOT find the element in this component, search other components in the project:
     * Search files that contain the text or classes from the HTML snippet
     * Elements may be in App.js, Home.js, Header.js, Footer.js, or other components
   - ⚠️ IMPORTANT: If the element is NOT in this component, you MUST state so clearly or search other files

2. FIX the text colour according to the background:
   - If background is LIGHT (white, light grey, light colours): use DARK text
     * style={{ color: '#000000' }} or color="#000000" or color="black"
   - If background is DARK (black, dark grey, dark colours): use LIGHT text
     * style={{ color: '#FFFFFF' }} or color="#FFFFFF" or color="white"

3. VALID FORMATS in React/JSX:
   - style={{ color: '#000000' }} (inline style)
   - color="#000000" (Chakra UI prop like <Text color="#000000">)
   - color="black" (Chakra UI prop with colour name)
   - If the element already has style={{ ... }}, add color inside the same object

4. IMPORTANT:
   - If the element uses Chakra UI (Text, Heading, Button, etc.), you can modify the color="..." prop
   - If the element is native HTML (<span>, <p>, <button>, etc.), use style={{ color: '...' }}
   - Do NOT change background colours, only text colour
   - Do NOT return the code unchanged if there are listed contrast violations

⚠️ Do NOT return the same code. You MUST make real changes to the colours."""
    
    prompt = f"""Fix ALL {total} WCAG A/AA violations in this React component.

COMPONENT: {component_path}

VIOLATIONS:
{violations_text}
{contrast_instructions}

QUICK RULES:
- color-contrast → adjust ONLY text colour (style={{ color: '...' }} or color="...") according to background
- aria-input-field-name / label → <label htmlFor="id"> or aria-label="text" on inputs/selects
- button-name → visible text or aria-label="action" on <button>
- link-name → descriptive text or aria-label="destination" on <a>
- image-alt / role-img-alt → alt="..." or aria-label="..." on images/visual roles
- frame-title → title="..." on <iframe>
- select-name → <label htmlFor> or aria-label on <select>
- target-size → padding / minWidth / minHeight for touch area (~44x44px)
- nested-interactive → avoid <button> inside <a> (and vice versa)

INSTRUCTIONS:
- Fix ONLY the elements listed in the violations list.
- PRECISE LOCATION: For each violation, find the EXACT element using:
  * Visible text from the HTML snippet (e.g. "Code", "Chat on whatsapp", "Save Contact")
  * CSS classes from the snippet (e.g. "btn-outline-light", "btn-success", "btn-outline-dark mx-1 d-flex")
  * Tag and element structure
- If you do NOT find the element in this component:
  * The element may be in another component (App.js, Home.js, Header.js, Footer.js, etc.)
  * Search the project for the text or classes from the HTML snippet
  * If you cannot access other components, state clearly that the element is not in this file
- Keep hooks, props, state and React logic unchanged.
- Do not change layout (width, height, margin, padding, display, position, flex, grid).
- Do not remove or add large JSX components; add/modify attributes on existing elements.
- ⚠️ CRITICAL: If contrast violations are listed, you MUST change the colours. Do NOT return the code unchanged.
- ⚠️ CRITICAL: If the element is NOT in this component, do NOT invent it. Search other files or state that it was not found.

FULL COMPONENT (CURRENT):
```jsx
{component_content}
```

Return ONLY the full corrected component, no explanations."""

    return prompt.strip()


def _get_specific_instruction_for_violation(violation_id: str, html_snippet: str, contrast_info: str) -> str:
    """Return a specific, concise instruction for each violation type."""
    v_lower = violation_id.lower()
    
    if "color-contrast" in v_lower:
        if contrast_info:
            # Extract contrast data in a simple way
            bg = "#ffffff"  # default
            if "Background color:" in contrast_info:
                try:
                    bg = contrast_info.split("Background color:")[1].split("\n")[0].strip()
                except Exception:
                    pass
            elif "Color de fondo:" in contrast_info:
                try:
                    bg = contrast_info.split("Color de fondo:")[1].split("\n")[0].strip()
                except:
                    pass
            recommended = "#000000" if any(c in bg.lower() for c in ["#ff", "#fff", "#00d1", "white", "light"]) else "#FFFFFF"
            return f"Add style={{'color': '{recommended}'}} to the element (background: {bg})"
        return "Add style={{'color': '#000000'}} or style={{'color': '#FFFFFF'}} according to background"
    
    if "aria-input-field-name" in v_lower or "label" in v_lower or "form-field" in v_lower:
        return "Add <label htmlFor=\"id\"> or aria-label=\"descriptive text\" to input/select/textarea"
    
    if "button-name" in v_lower:
        return "Add visible text inside the <button> or aria-label=\"action\" if it only has icons"
    
    if "link-name" in v_lower:
        return "Add descriptive text inside the <a> or aria-label=\"destination\" if it only has icons"
    
    if "image-alt" in v_lower or "img" in v_lower:
        return "Add alt=\"description\" or alt=\"\" if the image is decorative"
    
    if "frame-title" in v_lower:
        return "Add title=\"content description\" to the <iframe>"
    
    if "select-name" in v_lower:
        return "Add <label htmlFor=\"id\"> or aria-label=\"text\" to the <select>"
    
    if "target-size" in v_lower:
        return "Increase touch area (min 44x44px) with padding or minWidth/minHeight in style"
    
    if "nested-interactive" in v_lower:
        return "Separate interactive elements: no <button> inside <a>, no <a> inside <button>"
    
    if "aria-allowed-attr" in v_lower:
        return "Remove ARIA attributes not allowed for the element's role"
    
    if "aria-required-children" in v_lower:
        return "Add the required child elements for the role or change the role to a valid one"
    
    if "aria-valid-attr-value" in v_lower:
        return "Fix invalid ARIA attribute values (e.g. role=\"invalid\" → role=\"button\")"
    
    if "aria-toggle" in v_lower:
        return "Add aria-label=\"toggle state\" to the element with role=\"switch\" or role=\"checkbox\""
    
    return "Read the description and apply the minimum necessary fix"


def fix_react_components_with_axe_violations(
    issues_by_component: Dict[str, List[Dict]], project_root: Path, client, screenshot_paths: Optional[List[str]] = None
) -> Dict[str, Dict[str, str]]:
    """
    Use Axe information to ask the LLM to fix React components.
    
    This function is identical to Angular's fix_templates_with_axe_violations but for React.
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
                "You are an EXPERT in web accessibility (WCAG 2.2 A+AA) and React. "
                "Your MISSION is to fix ALL accessibility violations reported by Axe "
                "by modifying the full JSX component. "
                "🚨 CRITICAL: You MUST make real changes to the code. Do NOT return the same code. "
                "🚨 If there are contrast violations, you MUST add or modify style={{ color: '...' }} or color=\"...\" "
                "🚨 If there are aria-label, button-name, link-name violations, etc., you MUST add the required attributes. "
                "🚨 Keep React logic (hooks, props, state) intact. "
                "🚨 Do NOT change the responsive design - fixes must be visually invisible. "
                "🚨 For colour contrast, ONLY adjust text colour, do NOT change layout or backgrounds. "
                "🚨 If you return the same code unchanged, the fix FAILS completely. "
                "⚠️ IMPORTANT: If contrast errors are listed, you MUST change the colours. "
                "⚠️ If the code already has a colour but Axe reports an error, it means: "
                "   a) The colour is not being applied correctly (add !important or use inline style), OR "
                "   b) You are changing the wrong element. "
                "⚠️ Find the EXACT element using the 'Affected HTML fragment' and make sure you change the correct colour. "
                "⚠️ Do NOT return the code unchanged if contrast violations are reported."
            )
            
            print(f"[React + Axe] Fixing component based on Axe: {rel_path}")
            print(f"[React + Axe] Violations to fix: {len(issues)}")
            for i, issue in enumerate(issues, 1):
                violation_id = issue.get("violation", {}).get("id", "unknown")
                print(f"  {i}. {violation_id}")
            
            # Log prompt for debugging
            print(f"[React + Axe] 📝 Generated prompt (first 1500 chars):")
            print(prompt[:1500])
            print(f"[React + Axe] ... (total: {len(prompt)} chars)")
            
            # Log current code for comparison
            print(f"[React + Axe] 📄 Current code (first 500 chars):")
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
📸 SCREENSHOTS - CRITICAL FOR PRESERVING DESIGN:

I have taken screenshots of the application at different screen sizes (mobile, tablet, desktop) that show how the page REALLY looks before the fixes.

🚨 MANDATORY INSTRUCTIONS ABOUT THE SCREENSHOTS:
1. EXAMINE each screenshot in detail to understand:
   - The current visual design (layout, colours, spacing, distribution)
   - How content adapts at different screen sizes
   - Which elements are visible/hidden at each size
   - The application's overall visual style
   - The REAL background colours visible in the screenshots

2. FIX ALL contrast errors listed above, BUT:
   - KEEP the visual design you see in the screenshots
   - Do NOT change background colours, element sizes, or distribution shown in the images
   - For contrast errors: adjust ONLY the text colour based on the REAL background you see in the screenshots
   - If the background is LIGHT in the screenshots: use DARK text (#000000, #212121)
   - If the background is DARK in the screenshots: use LIGHT text (#FFFFFF, #F5F5F5)
   - Do NOT add new visible elements (use aria-label or sr-only instead)
   - Do NOT change display:none to display:block if that element is not visible in the screenshots
   - Respect the responsive design: if it looks a certain way on mobile, keep it that way

3. YOUR GOAL: Fix ALL contrast errors WITHOUT changing how the page looks in the screenshots.
   - Fixes should be visually "invisible"
   - Use minimal contrast adjustments based on the REAL backgrounds you see in the screenshots
   - The final design must look IDENTICAL to the screenshots, but accessible

The screenshots show the application BEFORE the fixes. Your job is to make it accessible while keeping that exact visual appearance.

🚨 CRITICAL - DO NOT BREAK RESPONSIVE:
- Do NOT change layout properties in style: width, height, margin, padding, display, position, flex, grid
- Do NOT modify className that affect responsive behaviour
- For contrast: ONLY change text colour, do NOT touch layout or backgrounds
- The design must look IDENTICAL on mobile, tablet and desktop after the fixes
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

            # CRITICAL VALIDATION: ensure LLM returned valid code (SAME AS ANGULAR)
            is_valid_response = True
            
            if corrected.strip().startswith("//") or corrected.strip().startswith("/*"):
                print(f"[React + Axe] ⚠️ LLM returned a comment instead of code for {rel_path}")
                is_valid_response = False
            
            if is_valid_response and not re.search(r'<\w+|import\s+|export\s+|function\s+|const\s+|class\s+', corrected):
                print(f"[React + Axe] ⚠️ LLM did not return valid React/JSX code for {rel_path}")
                is_valid_response = False
            
            if is_valid_response and len(corrected.strip()) < len(original_content.strip()) * 0.5:
                print(f"[React + Axe] ⚠️ La respuesta del LLM es demasiado corta para {rel_path} ({len(corrected)} vs {len(original_content)} chars)")
                is_valid_response = False

            # VALIDATION: ensure no new elements were added
            orig_tags = set(re.findall(r'<(\w+)', original_content))
            corr_tags = set(re.findall(r'<(\w+)', corrected)) if corrected else set()
            new_tags = corr_tags - orig_tags
            
            # Allowed tags that may be added (only <label> for inputs without label)
            allowed_new_tags = {'label'}
            problematic_new_tags = new_tags - allowed_new_tags
            
            if problematic_new_tags:
                print(f"[React + Axe] ⚠️ LLM added disallowed new elements: {problematic_new_tags}")
                print(f"[React + Axe] ⚠️ Changes will NOT be applied to avoid introducing errors")
                is_valid_response = False
            
            # COMPARAR Y APLICAR (MEJORADO - Similar a Angular pero para React/JSX)
            # Detect differences more robustly (including colour changes in different formats)
            
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
            
            # More robust comparison: normalise spaces but detect real changes
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
                    print(f"[React + Axe] ⚠️ LLM returned invalid code for {rel_path}")
                else:
                    print(f"[React + Axe] ⚠️ LLM returned the same code for {rel_path}")
                    # If contrast violations but no changes detected, show more info
                    has_contrast = any(issue.get("violation", {}).get("id", "") == "color-contrast" for issue in issues)
                    if has_contrast:
                        print(f"[React + Axe] ⚠️ HAY VIOLACIONES DE CONTRASTE PERO NO SE DETECTARON CAMBIOS")
                        print(f"[React + Axe] Colores en original: {sorted(orig_colors)}")
                        print(f"[React + Axe] Colores en corregido: {sorted(corr_colors)}")
                        print(f"[React + Axe] Estilos en original: {len(orig_styles)}")
                        print(f"[React + Axe] Estilos en corregido: {len(corr_styles)}")
                        print(f"[React + Axe] LLM probably did not apply the fixes")
                        print("[React + Axe] 💡 Suggestion: Check that the LLM added style={{ color: '...' }} "
                              "or modified the color=\"...\" prop)")

        except Exception as e:
            print(f"[React + Axe] ⚠️ Error fixing {rel_path}: {e}")
    
    return fixes


def _apply_react_accessibility_fixes(jsx_content: Optional[str]) -> Optional[str]:
    """Apply automatic accessibility fixes to JSX (same as Angular)."""
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
    """Fix common basic JSX syntax errors (same as Angular but for JSX)."""
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
    """Fix ARIA attribute syntax in JSX."""
    if not jsx_content:
        return jsx_content
    return jsx_content


def run_axe_on_react_app(base_url: str, run_path: str, suffix: str = "", take_screenshots_flag: bool = False) -> Tuple[Dict, List[str]]:
    """
    Run Axe on an already-running React app and return the results.
    """
    driver = None
    screenshot_paths = []
    
    try:
        driver = setup_driver()
        driver.get(base_url)
        
        if take_screenshots_flag:
            # Convert run_path to Path if it's a string
            run_path_obj = Path(run_path) if isinstance(run_path, str) else run_path
            # take_screenshots expects: driver, url, output_dir, prefix
            screenshot_paths = take_screenshots(driver, base_url, run_path_obj, prefix=f"screenshot{suffix}" if suffix else "screenshot")
        
        axe_results = run_axe_analysis(driver, base_url)
        
        return axe_results, screenshot_paths
    finally:
        if driver:
            driver.quit()


def process_react_project(project_path: str, client, run_path: str, serve_app: bool = False) -> List[str]:
    """
    Process a local React project (classic flow without Axe).
    NOTE: The Axe flow runs in main.py with --react-axe.
    """
    # This classic flow does not use Axe, only static analysis if implemented
    # The Axe flow is in main.py (_process_react_project_flow)
    return []
