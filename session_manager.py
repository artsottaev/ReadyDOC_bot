class SessionData:
    def __init__(self):
        self.document_type = None
        self.answers = {}
        self.is_complete = False

class SessionManager:
    def __init__(self):
        self.sessions = {}

    def get_session(self, user_id):
        if user_id not in self.sessions:
            self.sessions[user_id] = SessionData()
        return self.sessions[user_id]

    def clear_session(self, user_id):
        if user_id in self.sessions:
            del self.sessions[user_id]
