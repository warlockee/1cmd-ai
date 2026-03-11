# Demo Recording Script

Target length: ~70 seconds. Record on macOS with screen recording + Telegram on phone or simulator.

## Scene 1: Setup (10s)

- Show terminal: `pip install onecmd`
- Show terminal: `onecmd --apikey $BOT_TOKEN`
- Show "Bot started" message

## Scene 2: Manual Mode (15s)

- Open Telegram on phone
- Send `.list` -- show terminal list
- Send `.1` -- connect to terminal
- Type `htop` -- see it appear in terminal
- Show refresh -- live terminal output in Telegram

## Scene 3: AI Manager (30s) -- THE MONEY SHOT

- Send `.mgr` -- "Manager mode activated"
- Send "check disk space on all terminals"
  - AI runs `df -h` across terminals, summarizes results
- Send "the web server in terminal 2 seems slow, investigate"
  - AI reads terminal, runs diagnostic commands, reports findings
- Send "restart nginx and watch the logs for errors"
  - AI sends commands, monitors output, reports back

## Scene 4: Automation (15s)

- Send "every 5 minutes, check if the deploy pipeline is stuck"
  - AI sets up a recurring check
- Show notification: "Deploy completed successfully"

## Total: ~70 seconds

## Recording Tools

- **Screen**: macOS built-in screen recording (Cmd+Shift+5) or OBS
- **Phone**: Telegram on physical device or iOS Simulator
- **Terminal**: Use a clean terminal with a dark theme for contrast
- **Tip**: Pre-stage the environment so commands complete quickly during recording

## Post-Processing

1. Trim dead air and typing pauses
2. Add subtle captions for key moments
3. Export as:
   - **MP4** (1080p, <15MB) for GitHub README
   - **GIF** (720p, <5MB) for landing page hero
   - **asciicast** via `asciinema rec demo.cast` for terminal-only demo

## ASCII Demo Alternative (for GitHub README)

If video is not ready, create a terminal recording:

```bash
# Install asciinema
pip install asciinema

# Record
asciinema rec demo.cast

# Convert to SVG (embeddable, no hosting needed)
npx svg-term --in demo.cast --out docs/demo.svg --window --width 80 --height 24
```

## Screenshot Checklist

Capture these screenshots for the landing page and README:

- [ ] Telegram chat showing AI manager conversation
- [ ] Terminal list view (`.list` command output)
- [ ] AI auto-diagnosing a stuck process
- [ ] TOTP QR code setup screen
- [ ] Split view: terminal on left, Telegram on right
