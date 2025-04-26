
from docx import Document

def generate_doc_from_text(text, user_id):
    doc = Document()
    for line in text.split('\n'):
        doc.add_paragraph(line)
    path = f"/tmp/final_contract_{user_id}.docx"
    doc.save(path)
    return path
