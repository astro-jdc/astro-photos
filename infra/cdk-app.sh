#!/usr/bin/env sh
# Comando que el CLI de CDK usa para ejecutar la app (ver `app` en cdk.json).
#
# Existe por una razón concreta: `make synth` invoca el CLI por su ruta
# (`infra/.venv/bin/cdk`) sin activar el venv, así que un simple `python3 app.py`
# usaría el Python del sistema, que no tiene `aws_cdk` instalado. Aquí se prefiere
# el intérprete del venv si está, y si no (CI, donde se instala a nivel global) se
# cae al `python3` del PATH.
set -e

if [ -x "$(dirname "$0")/.venv/bin/python" ]; then
  exec "$(dirname "$0")/.venv/bin/python" "$(dirname "$0")/app.py" "$@"
fi

exec python3 "$(dirname "$0")/app.py" "$@"
