import json
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By

from utils.violation_utils import group_and_simplify_violations, flatten_violations
from utils.html_utils import convert_paths_to_absolute
from config.constants import NODE_CHUNK_SIZE

def generate_accessible_html_iteratively(original_html, axe_results, media_descriptions, client):
    print("Iniciando la generación de HTML por grupos de errores...")
    grouped_violations = group_and_simplify_violations(axe_results.get('violations', []))
    if not grouped_violations:
        print("No hay violaciones que corregir.")
        return original_html

    current_html = original_html
    descriptions_json = json.dumps(media_descriptions, indent=2)

    for violation_id, data in grouped_violations.items():
        nodes = data["nodes"]
        description = data["description"]

        for i in range(0, len(nodes), NODE_CHUNK_SIZE):
            node_chunk = nodes[i:i + NODE_CHUNK_SIZE]
            node_list_str = "\n".join(f"- {n}" for n in node_chunk)

            prompt = f"""
            Eres un experto desarrollador web especializado en accesibilidad (WCAG). Tu tarea es reescribir el siguiente CÓDIGO HTML ACTUAL para corregir MÚLTIPLES INSTANCIAS DE UN MISMO TIPO de error de accesibilidad.

            Tipo de Error: '{description}' (ID: {violation_id}).

            ELEMENTOS A CORREGIR:
            {node_list_str}

            MAPEO DE IMÁGENES:
            ```json
            {descriptions_json}
            ```

            HTML ORIGINAL:
            ```html
            {current_html}
            ```
            """

            try:
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": "Devuelves sólo el nuevo HTML completo."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.0,
                )

                new_html_chunk = response.choices[0].message.content.strip()
                if new_html_chunk.startswith("```html"):
                    new_html_chunk = new_html_chunk[7:]
                if new_html_chunk.endswith("```"):
                    new_html_chunk = new_html_chunk[:-3]

                if new_html_chunk and "<html>" in new_html_chunk.lower():
                    current_html = new_html_chunk
                    print("  > Lote corregido con éxito.")
                else:
                    print("  > Error: HTML inválido devuelto por la IA.")
            except Exception as e:
                print(f"  > Error con OpenAI: {e}")
                return current_html

    print("Generación iterativa completada.")
    return current_html


def generate_accessible_html_with_parser(original_html, axe_results, media_descriptions, client, base_url, driver):
    print("\n--- Iniciando Proceso de Corrección con Arquitectura Híbrida ---")
    
    soup = BeautifulSoup(original_html, 'html.parser')
    all_violations = flatten_violations(axe_results.get('violations', []))
    
    if not all_violations:
        print("No se encontraron violaciones procesables.")
        return original_html

    print(f"\n[Fase 1/3] Eliminando nodos no visibles...")
    violations_to_fix = []
    selectors_to_remove = set()
    for v in all_violations:
        try:
            is_visible = driver.find_element(By.CSS_SELECTOR, v['selector']).is_displayed()
            if is_visible:
                violations_to_fix.append(v)
            else:
                selectors_to_remove.add(v['selector'])
        except Exception:
            selectors_to_remove.add(v['selector'])
    for selector in selectors_to_remove:
        node = soup.select_one(selector)
        if node:
            node.decompose()
    print(f"Limpieza finalizada.")

    print(f"\n[Fase 2/3] Corrigiendo {len(violations_to_fix)} violaciones en elementos visibles...")
    
    images_in_soup = soup.find_all('img')
    for img_tag in images_in_soup:
        src = img_tag.get('src')
        if src and src in media_descriptions:
            img_tag['alt'] = media_descriptions[src]
            img_tag['title'] = media_descriptions[src]

    fixed_dot_containers = set()
    for violation in violations_to_fix:
        try:
            node_to_fix = soup.select_one(violation['selector'])
            if not node_to_fix: continue

            is_owl_control = False
            if 'button-name' in violation['description']:
                class_list = node_to_fix.get('class', [])
                if 'owl-prev' in class_list:
                    node_to_fix['aria-label'] = 'Previous slide'
                    is_owl_control = True
                elif 'owl-next' in class_list:
                    node_to_fix['aria-label'] = 'Next slide'
                    is_owl_control = True
                elif 'owl-dot' in class_list:
                    dots_container = node_to_fix.find_parent(class_='owl-dots')
                    if dots_container and id(dots_container) not in fixed_dot_containers:
                        for idx, dot in enumerate(dots_container.find_all('button', class_='owl-dot')):
                            dot['aria-label'] = f'Go to slide {idx + 1}'
                        fixed_dot_containers.add(id(dots_container))
                    is_owl_control = True 
            
            if is_owl_control:
                print(f"  > FIX (Heurístico): Aplicado aria-label a control de carrusel en '{violation['selector']}'")
                continue 
            print(f"  > FIX (IA): Procesando '{violation['selector']}' para '{violation['description']}'")
            original_fragment = str(node_to_fix)
            prompt = f"""**Tarea**: Corrige el `FRAGMENTO DE CÓDIGO HTML` basándote en la `DESCRIPCIÓN DEL ERROR`. Devuelve ÚNICAMENTE el fragmento de código HTML corregido.
            **DESCRIPCIÓN DEL ERROR**: {violation['description']}
            **FRAGMENTO A CORREGIR**: ```html\n{original_fragment}\n```"""
            
            response = client.chat.completions.create(model="gpt-4o", messages=[{"role": "system", "content": "Devuelves solo fragmentos de código HTML corregidos."}, {"role": "user", "content": prompt}], temperature=0.1)
            corrected_fragment_str = response.choices[0].message.content.strip().removeprefix("```html").removesuffix("```").strip()
            
            if corrected_fragment_str:
                new_node = BeautifulSoup(corrected_fragment_str, 'html.parser').find()
                if new_node:
                    node_to_fix.replace_with(new_node)
            
        except Exception as e:
            print(f"  > ERROR procesando '{violation['selector']}': {e}")
    print(f"\n[Fase 3/3] Convirtiendo rutas relativas a absolutas...")
    soup = convert_paths_to_absolute(soup, base_url)

    print("\n--- Proceso de Corrección Finalizado ---")
    return str(soup)