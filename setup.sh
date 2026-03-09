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
if [ ! -f "$SCRIPT_DIR/Makefile" ]; then
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

if [[ "$OS" == "Darwin" ]]; then
    # Step 1: Check Xcode CLI tools
    info "Checking for Xcode Command Line Tools..."
    if ! xcode-select -p &>/dev/null; then
        warn "Xcode Command Line Tools not found. Installing..."
        xcode-select --install
        echo ""
        echo "Please complete the installation dialog, then re-run this script."
        exit 1
    fi
    ok "Xcode Command Line Tools found."
elif [[ "$OS" == "Linux" ]]; then
    # Step 1: Check Linux dependencies
    info "Checking Linux build dependencies..."
    MISSING=""
    command -v gcc &>/dev/null || MISSING="$MISSING gcc"
    command -v make &>/dev/null || MISSING="$MISSING make"
    command -v curl &>/dev/null || MISSING="$MISSING curl"
    command -v tmux &>/dev/null || MISSING="$MISSING tmux"
    command -v pkg-config &>/dev/null || MISSING="$MISSING pkg-config"
    pkg-config --exists libcurl 2>/dev/null || MISSING="$MISSING libcurl-dev"
    pkg-config --exists sqlite3 2>/dev/null || MISSING="$MISSING libsqlite3-dev"

    if [ -n "$MISSING" ]; then
        warn "Missing packages:$MISSING"
        info "Installing..."
        if command -v apt-get &>/dev/null; then
            # Map package names for apt
            PKGS=""
            [[ " $MISSING " == *" gcc "* || " $MISSING " == *" make "* ]] && PKGS="$PKGS build-essential"
            [[ " $MISSING " == *" curl "* ]] && PKGS="$PKGS curl"
            [[ " $MISSING " == *" tmux "* ]] && PKGS="$PKGS tmux"
            [[ " $MISSING " == *" pkg-config "* ]] && PKGS="$PKGS pkg-config"
            [[ " $MISSING " == *" libcurl-dev "* ]] && PKGS="$PKGS libcurl4-openssl-dev"
            [[ " $MISSING " == *" libsqlite3-dev "* ]] && PKGS="$PKGS libsqlite3-dev"
            sudo apt-get update -qq && sudo apt-get install -y $PKGS
        elif command -v yum &>/dev/null; then
            PKGS=""
            [[ " $MISSING " == *" gcc "* ]] && PKGS="$PKGS gcc"
            [[ " $MISSING " == *" make "* ]] && PKGS="$PKGS make"
            [[ " $MISSING " == *" curl "* ]] && PKGS="$PKGS curl"
            [[ " $MISSING " == *" tmux "* ]] && PKGS="$PKGS tmux"
            [[ " $MISSING " == *" pkg-config "* ]] && PKGS="$PKGS pkgconf-pkg-config"
            [[ " $MISSING " == *" libcurl-dev "* ]] && PKGS="$PKGS libcurl-devel"
            [[ " $MISSING " == *" libsqlite3-dev "* ]] && PKGS="$PKGS sqlite-devel"
            sudo yum install -y $PKGS
        else
            err "Please install:$MISSING"
            exit 1
        fi
    fi
    ok "Build dependencies found."
else
    err "Unsupported OS: $OS"
    exit 1
fi

# Step 2: Build
info "Building onecmd..."
make clean -s 2>/dev/null || true
if make -s; then
    ok "Build successful."
else
    err "Build failed. Check errors above."
    exit 1
fi

# Step 3: API key setup
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

# Step 4: AI Manager setup
echo ""
echo -e "${BOLD}AI Manager${NC}"
echo ""
echo "  OneCmd includes an AI manager that monitors your terminals,"
echo "  answers questions, and executes tasks autonomously."
echo "  Requires a Google (Gemini) or Anthropic (Claude) API key."
echo ""
echo "  1) Google Gemini  (recommended — fast and free tier available)"
echo "  2) Anthropic Claude"
echo "  3) Skip for now"
echo ""
read -p "  Choose [1/2/3]: " -n 1 -r MGR_CHOICE
echo ""

LLM_KEY=""
LLM_VAR=""
if [[ "$MGR_CHOICE" == "1" ]]; then
    read -p "  Google API key: " LLM_KEY
    LLM_KEY=$(echo "$LLM_KEY" | tr -d '[:space:]')
    LLM_VAR="GOOGLE_API_KEY"
elif [[ "$MGR_CHOICE" == "2" ]]; then
    read -p "  Anthropic API key: " LLM_KEY
    LLM_KEY=$(echo "$LLM_KEY" | tr -d '[:space:]')
    LLM_VAR="ANTHROPIC_API_KEY"
fi

if [[ -n "$LLM_VAR" && -z "$LLM_KEY" ]]; then
    warn "No API key provided. Skipping AI manager."
    LLM_VAR=""
fi

if [[ -n "$LLM_VAR" ]]; then
    info "Setting up Python environment for AI manager..."
    if command -v python3 &>/dev/null; then
        python3 -m venv mgr/.venv 2>/dev/null || true
        if [ -f mgr/.venv/bin/pip ]; then
            mgr/.venv/bin/pip install -q -r mgr/requirements.txt
            ok "AI manager dependencies installed."
        else
            warn "Failed to create Python venv. AI manager unavailable."
            LLM_VAR=""
        fi
    else
        warn "python3 not found. AI manager requires Python 3."
        LLM_VAR=""
    fi
fi

# Step 5: Accessibility permission check (macOS only)
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

# Step 6: Create .env and launch script
ENV_FILE=".env"
{
    echo "# OneCmd environment — keep this file private"
    echo "TELEGRAM_BOT_TOKEN=$(tr -d '[:space:]' < apikey.txt)"
    if [[ -n "$LLM_VAR" ]]; then
        echo "$LLM_VAR=$LLM_KEY"
    fi
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
exec ./onecmd "$@"
RUNEOF
chmod +x run.sh
ok "Created run.sh"

# Step 7: Linux tmux reminder
if [[ "$OS" == "Linux" ]]; then
    echo ""
    echo -e "  ${YELLOW}Important:${NC} On Linux, onecmd controls tmux sessions."
    echo "  Make sure your work is running inside tmux before using .list"
    echo ""
    echo "  Quick start:"
    echo "    tmux new -s dev          # start a session"
    echo "    tmux new -s server -d    # start detached"
fi

# Step 8: Summary
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
