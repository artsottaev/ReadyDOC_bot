
import gspread
import os
import json
from oauth2client.service_account import ServiceAccountCredentials

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds_dict = json.loads(os.getenv('GOOGLE_CREDS_JSON'))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gs = gspread.authorize(creds)
sheet = gs.open('ReadyDoc MVP').sheet1

def save_row(user_id, doc_type, data_dict, mode="auto"):
    row = [str(user_id), doc_type] + list(data_dict.values()) + [mode]
    sheet.append_row(row)
