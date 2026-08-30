"""
WSGI entry point.

In a container gunicorn imports `app` from here. Running this file directly
starts the Flask development server, which is the supported path for local work
on machines without Docker.
"""

import os

from resumescreener import create_app

app = create_app()

if __name__ == "__main__":
    # Development only - never serve this to real traffic. Production runs under
    # gunicorn; see the Dockerfile.
    app.run(
        host=os.environ.get("FLASK_HOST", "127.0.0.1"),
        port=int(os.environ.get("FLASK_PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "1") == "1",
    )
