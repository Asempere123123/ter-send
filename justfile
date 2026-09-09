run *args:
    uv run ter-send {{args}}

build:
    uv run pyinstaller --onefile --name ter-send --collect-all cantools --collect-all can --hidden-import=textual src/ter_send/__init__.py

install: build
    sudo install -m 755 dist/ter-send /usr/local/bin/ter-send
