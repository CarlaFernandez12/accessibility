# 🌐 Mejorador Automático de Páginas Web para Todos

![Python](https://img.shields.io/badge/Python-3.8%2B-blue?style=for-the-badge&logo=python)
![OpenAI](https://img.shields.io/badge/OpenAI-API-green?style=for-the-badge&logo=openai)
![Selenium](https://img.shields.io/badge/Selenium-Framework-orange?style=for-the-badge&logo=selenium)
![WCAG](https://img.shields.io/badge/WCAG-2.0-purple?style=for-the-badge)

> ¿Qué hace este programa? En palabras sencillas, es una herramienta que hace que las páginas web sean más fáciles de usar para todas las personas, especialmente para aquellas con alguna discapacidad, y lo hace de forma automática usando Inteligencia Artificial.

## 📋 ¿Qué encontrarás en este documento?

- [💡 ¿Para qué sirve?](#-para-qué-sirve)
- [✨ ¿Qué puede hacer?](#-qué-puede-hacer)
- [🔧 ¿Qué necesitas para usarlo?](#-qué-necesitas-para-usarlo)
- [⚙️ ¿Cómo instalarlo?](#️-cómo-instalarlo)
- [🚀 ¿Cómo usarlo?](#-cómo-usarlo)
- [📊 ¿Cómo funciona por dentro?](#-cómo-funciona-por-dentro)
- [📁 ¿Dónde se guardan los resultados?](#-dónde-se-guardan-los-resultados)
- [🛠️ ¿Qué tecnología usa?](#️-qué-tecnología-usa)

## 💡 ¿Para qué sirve?

Imagina que tienes una página web y quieres asegurarte de que cualquier persona pueda usarla, independientemente de si:
- Usa un lector de pantalla porque tiene dificultades visuales
- No puede usar el ratón y navega solo con teclado
- Tiene dificultades para distinguir ciertos colores
- O cualquier otra situación que pueda dificultar el uso de la web

Este programa analiza automáticamente tu página web y:
1. Encuentra problemas que podrían hacer difícil su uso
2. Los corrige automáticamente cuando es posible
3. Te muestra un informe detallado de las mejoras realizadas

## ✨ ¿Qué puede hacer?

- **🔍 Encuentra Problemas Automáticamente**: 
  - Revisa toda la página web buscando elementos que podrían ser difíciles de usar
  - Comprueba que cumple con las normas internacionales de accesibilidad (WCAG 2.0)

- **🤖 Usa Inteligencia Artificial**: 
  - Describe las imágenes para personas que no pueden verlas
  - Sugiere mejoras en los textos para que sean más claros
  - Corrige problemas en el código de la página

- **💾 Es Eficiente**: 
  - Guarda las descripciones de imágenes para no tener que generarlas de nuevo
  - Trabaja rápido y de forma inteligente

- **📊 Te Mantiene Informado**: 
  - Crea informes fáciles de entender
  - Muestra el "antes y después" de las mejoras
  - Te explica qué ha cambiado y por qué

## 🔧 ¿Qué necesitas para usarlo?

1. **En tu ordenador debe estar instalado**:
   - Python (versión 3.8 o más nueva)
   - Google Chrome
   - Conexión a Internet

2. **Necesitarás también**:
   - Una clave de API de OpenAI (es como una contraseña especial para usar la Inteligencia Artificial)
   - Un poco de espacio en tu disco duro para guardar los resultados

## ⚙️ ¿Cómo instalarlo?

1. **Primer paso: Obtener el programa**
   ```bash
   git clone https://github.com/CarlaFernandez12/accessibility.git
   cd <NOMBRE-DEL-DIRECTORIO>
   ```
   *(Tu supervisor te proporcionará los valores correctos para reemplazar lo que está entre < >)*

2. **Segundo paso: Preparar el entorno**
   ```bash
   # Si usas Windows, escribe esto en la terminal:
   python -m venv venv
   venv\Scripts\activate

   # Si usas Mac o Linux, escribe esto en su lugar:
   python -m venv venv
   source venv/bin/activate
   ```

3. **Tercer paso: Instalar lo necesario**
   ```bash
   pip install -r requirements.txt
   ```

4. **Cuarto paso: Configurar la clave de API**
   - Crea un archivo nuevo llamado `.env` en la carpeta principal
   - Dentro del archivo escribe:
     ```
     OPENAI_API_KEY="tu-clave-api-aquí"
     ```
   - Guarda el archivo

## 🚀 ¿Cómo usarlo?

1. **Iniciar el programa**
   ```bash
   python main.py
   ```

2. **Usar el programa**
   - Cuando te lo pida, escribe la dirección web que quieres analizar
   - Espera mientras el programa:
     - Analiza la página
     - Encuentra problemas
     - Aplica mejoras
     - Genera el informe

3. **Ver los resultados**
   - El programa te dirá dónde encontrar los resultados
   - Podrás ver:
     - Qué problemas encontró
     - Qué mejoras hizo
     - Cómo quedó la página después de las mejoras

## 📊 ¿Cómo funciona por dentro?

El programa sigue estos pasos:

1. **🔍 Análisis**
   - Visita la página web que le indicas
   - Busca problemas de accesibilidad
   - Hace una lista de todo lo que necesita mejorar

2. **📸 Mejora de imágenes**
   - Descarga las imágenes de la página
   - Usa IA para describirlas
   - Guarda las descripciones para usarlas después

3. **🔧 Mejoras en la página**
   - Añade las descripciones a las imágenes
   - Corrige problemas en la estructura
   - Mejora textos poco claros

4. **📈 Creación del informe**
   - Compara cómo era la página antes y después
   - Cuenta cuántas mejoras se hicieron
   - Crea un informe fácil de entender

## 📁 ¿Dónde se guardan los resultados?

El programa crea una carpeta llamada `results` con esta estructura:

```
results/
└── nombre_de_la_web/
    └── fecha_y_hora/
        ├── initial_report.json    (problemas encontrados)
        ├── accessible_page.html   (página mejorada)
        ├── final_report.json      (mejoras realizadas)
        └── comparison_report.html (informe comparativo)
```

## 🛠️ ¿Qué tecnología usa?

- **🐍 Python**: El lenguaje de programación principal
- **🤖 OpenAI**: La Inteligencia Artificial que ayuda a mejorar la página
- **🌐 Selenium**: Herramienta para navegar automáticamente por la web
- **🎯 axe-core**: Detector de problemas de accesibilidad
- **🔍 BeautifulSoup4**: Herramienta para leer y modificar páginas web
- **📝 Jinja2**: Creador de informes bonitos y claros

---

📄 **Licencia**: MIT  
👩‍💻 **Autora**: Carla

### 🤝 ¿Necesitas ayuda?

Si tienes alguna duda o problema:
1. Revisa que has seguido todos los pasos correctamente
2. Asegúrate de que tu ordenador cumple con los requisitos
3. Contacta con el equipo de soporte si necesitas más ayuda

### 📝 Notas importantes

- El programa necesita conexión a Internet para funcionar
- Algunas mejoras pueden tardar unos minutos en completarse
- Es normal que el navegador se abra y cierre solo mientras el programa trabaja
- Los informes se guardan automáticamente para que puedas consultarlos cuando quieras 