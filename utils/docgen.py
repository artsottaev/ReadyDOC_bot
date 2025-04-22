
from docx import Document

def normalize(value):
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items())
    elif isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)

def generate_doc(doc_type, data, user_id):
    with open(f'templates/{doc_type}.md', encoding='utf-8') as f:
        text = f.read()
    for key, value in data.items():
        text = text.replace(f'{{{{{key}}}}}', normalize(value))
    doc = Document()
    for line in text.split('\n'):
        doc.add_paragraph(line)
    output = f'/tmp/{user_id}_{doc_type}.docx'
    doc.save(output)
    return output, text
