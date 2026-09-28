from flask import Blueprint, Flask, request
from __init__ import app
from flask_restful import Api, Resource
import subprocess, tempfile, os, requests

python_exec_api = Blueprint('python_exec_api', __name__, url_prefix='/run')

api = Api(python_exec_api)

runner_port = app.config['RUNNER_PORT']
# based on docker configured name
RUNNER_URL = f'http://code_runner:{runner_port}/python'

class PythonExec(Resource):
    def post(self):
        data = request.get_json(silent=True) or {}
        code = data.get("code", "")

        if not code.strip():
            return {"output": "⚠️ No code provided."}, 400

        is_production = os.environ.get("IS_PRODUCTION", "false").lower() == "true"

        if is_production:
            return _execute_remote(data)
        # might have to update this in future; could be vuln
        # skipping verbose check
        else:
            return _execute_local(code)


def _execute_local(code):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as tmp:
        tmp.write(code.encode())
        tmp.flush()

        try:
            result = subprocess.run(
                ["python3", tmp.name],
                capture_output=True,
                text=True,
                timeout=5,
                cwd="/tmp",  # Force working directory to /tmp
                env={"HOME": "/tmp", "PATH": "/usr/bin:/usr/local/bin"}  # Restricted environment
            )
            output = result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            output = "Execution timed out (5 s limit)."
        except Exception as e:
            output = f"Error running code: {str(e)}"
        finally:
            os.unlink(tmp.name)

    return {"output": output}

def _execute_remote(data):
    try:
        response = requests.post(
            RUNNER_URL,
            json=data,
            timeout=10
        )

        return response.json(), response.status_code

    except requests.Timeout:
        return {"output": "Runner timed out."}, 504

    except requests.RequestException as e:
        return {
            "output": f"Could not connect to code runner: {str(e)}"
        }, 502


api.add_resource(PythonExec, "/python")
