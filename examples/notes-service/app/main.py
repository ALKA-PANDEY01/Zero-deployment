import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class NotesHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ready":
            body = b"ready"
            status = 200
        elif self.path == "/":
            body = b"Notes service is running"
            status = 200
        else:
            body = b"not found"
            status = 404

        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        print(format_string % args, flush=True)


port = int(os.getenv("APP_PORT", "8081"))
server = ThreadingHTTPServer(("0.0.0.0", port), NotesHandler)
server.serve_forever()
