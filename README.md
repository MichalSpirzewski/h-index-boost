# RefBase

Self-hosted, no-login shared reference library for a research group. See [CLAUDE.md](CLAUDE.md) for full project scope and conventions.

## Running the app

```bash
./scripts/run.sh
```

This detects which machine you're on, activates the matching conda environment, and starts the app with `uvicorn`. On this dev machine it serves at [http://127.0.0.1:8000](http://127.0.0.1:8000) with `--reload`.

Extra args are passed through to uvicorn, e.g. `./scripts/run.sh --port 9000`.

### First-time environment setup

The script expects a conda env named `refbase` with the project dependencies installed:

```bash
source ~/miniforge3/bin/activate
conda create -y -n refbase python=3.11
conda activate refbase
pip install -r requirements.txt -r requirements-dev.txt
```

## Running tests

```bash
source ~/miniforge3/bin/activate refbase
pytest
```
