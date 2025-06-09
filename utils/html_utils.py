from urllib.parse import urljoin

def convert_paths_to_absolute(soup, base_url):
    tags_attributes = {
        'a': 'href',
        'link': 'href',
        'script': 'src',
        'img': 'src',
        'source': 'src',
        'iframe': 'src',
        'form': 'action',
    }

    print("\n[Paso 3/3] Convirtiendo rutas relativas a absolutas...")
    converted_count = 0
    for tag_name, attribute_name in tags_attributes.items():
        for tag in soup.find_all(tag_name):
            path = tag.get(attribute_name)
            if path and not path.startswith(('http://', 'https://', '#', 'data:', 'mailto:', 'tel:')):
                absolute_path = urljoin(base_url, path)
                tag[attribute_name] = absolute_path
                converted_count += 1
    
    print(f"Se han convertido {converted_count} rutas a absolutas.")
    return soup
