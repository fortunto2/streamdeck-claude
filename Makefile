.PHONY: run stop dev install

install:
	brew install hidapi
	uv sync

run:
	uv run streamdeck-claude

dev:
	uv run python -m src.daemon --verbose

stop:
	pkill -f "streamdeck-claude" || true
