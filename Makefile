PLIST_LABEL := com.streamdeck.dashboard
PLIST_SRC   := com.streamdeck.dashboard.plist
PLIST_DST   := $(HOME)/Library/LaunchAgents/$(PLIST_LABEL).plist

.PHONY: install run dev dashboard restart stop kill ps status logs install-daemon uninstall-daemon restart-daemon

install:
	brew install hidapi
	uv sync

# ── run modes ────────────────────────────────────────────────────────

run:
	uv run streamdeck-claude

dev:
	uv run python -m src.daemon --verbose

dashboard:
	uv run python scripts/dashboard.py

restart:
	@$(MAKE) kill
	@sleep 1
	@echo "Starting dashboard..."
	@nohup uv run python scripts/dashboard.py > /tmp/streamdeck-dashboard.log 2>&1 &
	@echo "Dashboard restarted. PID: $$!"

# ── process management ───────────────────────────────────────────────

stop:
	@pkill -f "streamdeck-claude" 2>/dev/null && echo "stopped streamdeck-claude" || true

kill:
	@echo "Killing all Stream Deck processes..."
	@pkill -f "scripts/(arcade|dashboard|snake_game|sequence_game|empire_game|beaver_game|simon_game|reaction_game|memory_game|breakout_game|nback_game|pattern_game|mathseq_game|quickmath_game|numgrid_game|bunny_game|invaders_game|lights_game|dodge_game|mines_game|colony_game|dungeon_game|factory_game|tower_game|trader_game|crypto_game|crypto_real_game)" 2>/dev/null && echo "  killed game scripts" || true
	@pkill -f "streamdeck-claude" 2>/dev/null && echo "  killed streamdeck-claude" || true
	@echo "Done."

ps:
	@echo "Stream Deck processes:"
	@ps aux | grep -E "streamdeck-claude|scripts/[a-z_]+\.py" | grep -v grep || echo "  (none)"

# ── launchd daemon ───────────────────────────────────────────────────

install-daemon:
	@echo "Installing Stream Deck dashboard daemon..."
	sed 's|__CWD__|$(PWD)|g; s|__UV__|$(shell which uv)|g; s|__HOME__|$(HOME)|g' \
	  $(PLIST_SRC) > $(PLIST_DST)
	launchctl bootstrap gui/$$(id -u) $(PLIST_DST) 2>/dev/null || launchctl load $(PLIST_DST)
	@echo "Daemon installed. Logs: /tmp/streamdeck-dashboard.log"
	@echo "  make status    — check daemon"
	@echo "  make logs      — view logs"

uninstall-daemon:
	@echo "Removing Stream Deck dashboard daemon..."
	launchctl bootout gui/$$(id -u)/$(PLIST_LABEL) 2>/dev/null || launchctl unload $(PLIST_DST) 2>/dev/null || true
	rm -f $(PLIST_DST)
	@echo "Done."

restart-daemon:
	launchctl kickstart -k gui/$$(id -u)/$(PLIST_LABEL) 2>/dev/null || \
	  (launchctl unload $(PLIST_DST) 2>/dev/null; launchctl load $(PLIST_DST))
	@echo "Daemon restarted."

status:
	@launchctl print gui/$$(id -u)/$(PLIST_LABEL) 2>/dev/null | head -20 || echo "Daemon not installed. Run: make install-daemon"

logs:
	@tail -30 /tmp/streamdeck-dashboard.log 2>/dev/null || echo "No logs yet."
