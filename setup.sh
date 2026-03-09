#!/bin/bash
set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BLUE}=>${NC} $1"; }
ok()    { echo -e "${GREEN}=>${NC} $1"; }
warn()  { echo -e "${YELLOW}=>${NC} $1"; }
err()   { echo -e "${RED}=>${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"

# Auto-clone if running via curl (not inside the repo)
if [ ! -f "$SCRIPT_DIR/pyproject.toml" ]; then
    info "Cloning onecmd..."
    git clone https://github.com/warlockee/1cmd-ai.git
    cd 1cmd-ai
    exec ./setup.sh "$@" < /dev/tty
fi

cd "$SCRIPT_DIR"

echo -e "${BOLD}"
echo "                                      "
echo "   ___  _ __   ___  ___ _ __ ___   __| |"
echo "  / _ \| '_ \ / _ \/ __| '_ \` _ \ / _\` |"
echo " | (_) | | | |  __/ (__| | | | | | (_| |"
echo "  \___/|_| |_|\___|\___|_| |_| |_|\__,_|"
echo ""
echo -e "${NC}  Control your terminal from Telegram"
echo ""

OS="$(uname)"

# Step 1: Check Python 3.11+
info "Checking Python..."
if ! command -v python3 &>/dev/null; then
    err "python3 not found. Please install Python 3.11 or later."
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    err "Python 3.11+ required (found $PY_VERSION)."
    exit 1
fi
ok "Python $PY_VERSION found."

# Step 2: Platform-specific checks
if [[ "$OS" == "Linux" ]]; then
    if ! command -v tmux &>/dev/null; then
        warn "tmux not found. Installing..."
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y tmux
        elif command -v yum &>/dev/null; then
            sudo yum install -y tmux
        else
            err "Please install tmux manually."
            exit 1
        fi
    fi
    ok "tmux found."
elif [[ "$OS" == "Darwin" ]]; then
    info "macOS detected. Accessibility permission may be required."
fi

# Step 3: Create venv and install
info "Setting up Python environment..."
python3 -m venv .venv

if [[ "$OS" == "Darwin" ]]; then
    .venv/bin/pip install -q ".[macos,dev]"
else
    .venv/bin/pip install -q ".[dev]"
fi
ok "Dependencies installed."

# Step 4: API key setup
echo ""
if [ -f apikey.txt ]; then
    EXISTING_KEY=$(tr -d '[:space:]' < apikey.txt)
    ok "Found existing API key in apikey.txt"
    echo -e "   Current key: ${BOLD}${EXISTING_KEY:0:10}...${NC}"
    echo ""
    read -p "   Keep this key? [Y/n] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Nn]$ ]]; then
        rm apikey.txt
    fi
fi

if [ ! -f apikey.txt ]; then
    echo -e "${BOLD}Telegram Bot Setup${NC}"
    echo ""
    echo "  1) I already have a bot token for this machine"
    echo "  2) I need to create a new bot"
    echo ""
    read -p "  Choose [1/2]: " -n 1 -r BOT_CHOICE
    echo ""
    echo ""

    if [[ "$BOT_CHOICE" == "2" ]]; then
        echo "  To create a new bot:"
        echo ""
        echo "  1. Open Telegram and message @BotFather"
        echo "  2. Send /newbot"
        echo "  3. Name it after this machine (e.g. 'My Server Terminal')"
        echo "  4. Copy the API token"
        echo ""
    fi

    read -p "  Paste your bot API token: " API_KEY
    API_KEY=$(echo "$API_KEY" | tr -d '[:space:]')

    if [ -z "$API_KEY" ]; then
        err "No API key provided."
        exit 1
    fi

    # Validate the token
    info "Validating token..."
    if ! RESPONSE=$(curl -s --connect-timeout 10 "https://api.telegram.org/bot${API_KEY}/getMe"); then
        err "Could not reach Telegram API. Check your internet connection."
        exit 1
    fi
    if echo "$RESPONSE" | grep -q '"ok":true'; then
        BOT_USERNAME=$(echo "$RESPONSE" | grep -o '"username":"[^"]*"' | cut -d'"' -f4)
        ok "Token valid! Bot: @${BOT_USERNAME}"
        echo "$API_KEY" > apikey.txt
        chmod 600 apikey.txt
    else
        err "Invalid token. Please check and try again."
        exit 1
    fi
fi

# Step 5: AI Manager setup
echo ""
echo -e "${BOLD}AI Manager${NC}"
echo ""
echo "  OneCmd includes an AI manager that monitors your terminals,"
echo "  answers questions, and executes tasks autonomously."
echo "  You can configure one or both AI providers."
echo "  If both are set, Gemini is used by default with Claude as fallback."
echo ""

GOOGLE_KEY=""
ANTHROPIC_KEY=""

read -p "  Google API key (Enter to skip): " GOOGLE_KEY
GOOGLE_KEY=$(echo "$GOOGLE_KEY" | tr -d '[:space:]')

read -p "  Anthropic API key (Enter to skip): " ANTHROPIC_KEY
ANTHROPIC_KEY=$(echo "$ANTHROPIC_KEY" | tr -d '[:space:]')

HAS_LLM=""
[[ -n "$GOOGLE_KEY" ]] && HAS_LLM=1
[[ -n "$ANTHROPIC_KEY" ]] && HAS_LLM=1

if [[ -z "$HAS_LLM" ]]; then
    warn "No API keys provided. AI manager will be unavailable."
fi

# Step 6: Accessibility permission check (macOS only)
if [[ "$OS" == "Darwin" ]]; then
    echo ""
    info "Checking Accessibility permission..."
    echo ""
    echo "  OneCmd needs Accessibility permission to:"
    echo "  - Read terminal window text"
    echo "  - Send keystrokes to terminal windows"
    echo "  - List and focus terminal windows"
    echo ""
    echo "  If prompted by macOS, click 'Allow' or add your terminal app"
    echo "  (iTerm2, Terminal, etc.) in:"
    echo "  System Settings > Privacy & Security > Accessibility"
    echo ""
fi

# Step 7: Create .env and launch script
ENV_FILE=".env"
{
    echo "# OneCmd environment — keep this file private"
    echo "TELEGRAM_BOT_TOKEN=$(tr -d '[:space:]' < apikey.txt)"
    [[ -n "$GOOGLE_KEY" ]] && echo "GOOGLE_API_KEY=$GOOGLE_KEY"
    [[ -n "$ANTHROPIC_KEY" ]] && echo "ANTHROPIC_API_KEY=$ANTHROPIC_KEY"
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "Created .env (mode 600)"

cat > run.sh << 'RUNEOF'
#!/bin/bash
cd "$(dirname "$0")"
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi
exec .venv/bin/onecmd "$@"
RUNEOF
chmod +x run.sh
ok "Created run.sh"

# Step 8: Linux tmux reminder
if [[ "$OS" == "Linux" ]]; then
    echo ""
    echo -e "  ${YELLOW}Important:${NC} On Linux, onecmd controls tmux sessions."
    echo "  Make sure your work is running inside tmux before using .list"
    echo ""
    echo "  Quick start:"
    echo "    tmux new -s dev          # start a session"
    echo "    tmux new -s server -d    # start detached"
fi

# Step 9: Summary
echo ""
echo -e "${GREEN}${BOLD}Setup complete!${NC}"
echo ""
echo "  To start onecmd:"
echo ""
echo -e "    ${BOLD}cd $SCRIPT_DIR && ./run.sh${NC}"
echo ""
echo "  Then open Telegram and message your bot."
echo "  The first user to message becomes the owner."
echo ""
echo "  Bot commands:"
echo "    .list    - List terminal sessions"
echo "    .1 .2 .. - Connect to a session"
echo "    .mgr     - Toggle AI manager mode"
echo "    .help    - Show all commands"
echo ""
echo -e "  ${YELLOW}Tip:${NC} To run in the background:"
echo -e "    ${BOLD}nohup ./run.sh &${NC}"
echo ""

read -p "  Start onecmd now? [Y/n] " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    echo ""
    info "Starting onecmd..."
    exec ./run.sh
fi
