from flask import Flask, jsonify, request
import subprocess, tempfile, os

runner = Flask(__name__)

@runner.post("/python")
def run_python():
    data = request.get_json()
    code = data.get("code", "")
    print("got request, running")

    if not code.strip():
        return {"output": "No code provided."}, 400

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

@runner.post("/javascript")
def run_javascript():
    data = request.get_json()
    code = data.get("code", "")

    if not code.strip():
        return {"output": "No code provided."}, 400

    strict_code = '"use strict";\n' + code

    with tempfile.NamedTemporaryFile(delete=False, suffix=".js") as tmp:
        tmp.write(strict_code.encode())
        tmp.flush()

        try:
            result = subprocess.run(
                ["node", tmp.name],
                capture_output=True,
                text=True,
                timeout=5,
                cwd="/tmp",  # Force working directory to /tmp
                env={"HOME": "/tmp", "PATH": "/opt/homebrew/bin:/usr/bin:/usr/local/bin"}  # Restricted environment (includes macOS Homebrew path)
            )
            output = result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            output = "Execution timed out (5 s limit)."
        except Exception as e:
            output = f"Error running JavaScript: {str(e)}"
        finally:
            os.unlink(tmp.name)

    return {"output": output}

if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("RUNNER_PORT", "8591"))
    print(f"** Server running: http://localhost:{port}")  # Pretty link
    runner.run(debug=True, host=host, port=port, use_reloader=False)
