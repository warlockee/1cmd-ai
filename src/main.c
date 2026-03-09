/*
 * main.c - Orchestrator for onecmd Telegram bot
 *
 * Contains global variable definitions and the main() entry point.
 * Reads like a recipe: parse args -> init TOTP -> start manager -> run bot.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <pthread.h>

#include "types.h"
#include "bot.h"
#include "state.h"
#include "commands.h"
#include "totp.h"
#include "terminal_io.h"
#include "manager_ipc.h"

/* ============================================================================
 * Global state (declared extern in types.h / used by other modules)
 * ========================================================================= */

TermInfo *TermList = NULL;
int TermCount = 0;
int Connected = 0;
char ConnectedId[128] = {0};
pid_t ConnectedPid = 0;
char ConnectedName[128] = {0};
char ConnectedTitle[256] = {0};
int DangerMode = 0;

/* Internal state shared across modules. */
pthread_mutex_t RequestLock = PTHREAD_MUTEX_INITIALIZER;
pthread_mutex_t MgrLock = PTHREAD_MUTEX_INITIALIZER;
volatile sig_atomic_t ShutdownRequested = 0;
int WeakSecurity = 0;             /* If 1, skip all OTP logic. */
int EnableOtp = 0;                /* If 1, generate TOTP secret on first run. */
int Authenticated = 0;            /* Whether OTP has been verified. */
time_t LastActivity = 0;          /* Last time owner sent a valid command. */
int OtpTimeout = 300;             /* Timeout in seconds (default 5 min). */

int64_t TrackedMsgIds[MAX_TRACKED_MSGS];
int TrackedMsgCount = 0;

/* LLM Manager Agent (onecmd-mgr) */
int MgrMode = 0;                  /* 1 if in manager conversation mode. */
pid_t MgrPid = -1;                /* PID of mgr child process. */
int MgrWriteFd = -1;              /* Pipe: onecmd writes to mgr stdin. */
int MgrReadFd = -1;               /* Pipe: onecmd reads mgr stdout. */
pthread_t MgrReaderThread;
int MgrReaderRunning = 0;
char MgrPath[512] = {0};          /* Path to onecmd-mgr script. */

/* ============================================================================
 * Signal Handlers
 * ========================================================================= */

static void shutdown_handler(int sig) {
    (void)sig;
    ShutdownRequested = 1;
    if (MgrPid > 0) kill(MgrPid, SIGTERM);
}

/* ============================================================================
 * Main
 * ========================================================================= */

int main(int argc, char **argv) {
    setlinebuf(stdout);  /* Force line-buffered so log output appears in real time. */

    /* Parse our custom flags. */
    const char *dbfile = "./mybot.sqlite";
    const char *apikey = NULL;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--dangerously-attach-to-any-window") == 0) {
            DangerMode = 1;
            printf("DANGER MODE: All windows will be visible.\n");
        } else if (strcmp(argv[i], "--use-weak-security") == 0) {
            WeakSecurity = 1;
            printf("WARNING: OTP authentication disabled.\n");
        } else if (strcmp(argv[i], "--enable-otp") == 0) {
            EnableOtp = 1;
        } else if (strcmp(argv[i], "--dbfile") == 0 && i+1 < argc) {
            dbfile = argv[++i];
        } else if (strcmp(argv[i], "--apikey") == 0 && i+1 < argc) {
            apikey = argv[++i];
        } else if (strcmp(argv[i], "--mgr") == 0 && i+1 < argc) {
            strncpy(MgrPath, argv[++i], sizeof(MgrPath) - 1);
            MgrPath[sizeof(MgrPath) - 1] = '\0';
            printf("MGR: Manager script: %s\n", MgrPath);
        }
    }

    /* Auto-detect manager script if not explicitly provided. */
    int mgr_explicit = (MgrPath[0] != '\0');
    if (!mgr_explicit) {
        FILE *mf = fopen("mgr/main.py", "r");
        if (mf) {
            fclose(mf);
            strncpy(MgrPath, "mgr/main.py", sizeof(MgrPath) - 1);
            printf("MGR: Auto-detected manager script: %s\n", MgrPath);
        }
    }

    /* -------------------------------------------------------------------
     * First-time-friendly: refuse to start if required keys are missing.
     * ----------------------------------------------------------------- */
    int missing = 0;

    /* Check Telegram bot token (--apikey or apikey.txt). */
    if (apikey == NULL) {
        FILE *fp = fopen("apikey.txt", "r");
        if (fp) { fclose(fp); }
        else {
            fprintf(stderr,
                "\n  ERROR: Telegram bot token not provided.\n"
                "         Use --apikey <TOKEN> or create an apikey.txt file.\n"
                "         Get a token from @BotFather on Telegram.\n\n");
            missing = 1;
        }
    }

    /* Check LLM API key when manager is configured (Anthropic or Google). */
    if (MgrPath[0] != '\0') {
        const char *anthropic_key = getenv("ANTHROPIC_API_KEY");
        const char *google_key = getenv("GOOGLE_API_KEY");
        int has_anthropic = (anthropic_key != NULL && anthropic_key[0] != '\0');
        int has_google = (google_key != NULL && google_key[0] != '\0');
        if (!has_anthropic && !has_google) {
            if (mgr_explicit) {
                /* User explicitly requested --mgr: hard error. */
                fprintf(stderr,
                    "  ERROR: No LLM API key set. Set one of:\n"
                    "         ANTHROPIC_API_KEY  (for Claude)  — https://console.anthropic.com/\n"
                    "         GOOGLE_API_KEY     (for Gemini)  — https://aistudio.google.com/\n\n");
                missing = 1;
            } else {
                /* Auto-detected: just disable manager and continue. */
                printf("MGR: No LLM API key set. Manager disabled.\n");
                MgrPath[0] = '\0';
            }
        }
    }

    if (missing) {
        fprintf(stderr,
            "  Usage: ANTHROPIC_API_KEY=sk-... ./onecmd --apikey <TELEGRAM_TOKEN> [options]\n"
            "     or: GOOGLE_API_KEY=... ./onecmd --apikey <TELEGRAM_TOKEN> [options]\n\n"
            "  Options:\n"
            "    --apikey <token>    Telegram bot token (from @BotFather)\n"
            "    --enable-otp        Enable OTP authentication (generates TOTP secret on first run)\n"
            "    --use-weak-security Disable OTP authentication (even if previously configured)\n"
            "    --mgr <path>       Path to AI manager script (requires ANTHROPIC_API_KEY or GOOGLE_API_KEY)\n"
            "    --dbfile <path>    SQLite database path (default: ./mybot.sqlite)\n\n");
        return 1;
    }

    /* Ignore SIGPIPE so writes to broken mgr pipe return EPIPE instead. */
    signal(SIGPIPE, SIG_IGN);

    /* Graceful shutdown on SIGTERM/SIGINT. */
    signal(SIGTERM, shutdown_handler);
    signal(SIGINT, shutdown_handler);

    /* TOTP setup: check/generate secret before starting the bot. */
    totp_setup(dbfile);

    /* Start manager eagerly so it's warm when the user first messages. */
    if (MgrPath[0] != '\0') {
        mgr_start();
    }

    /* Triggers: respond to all private messages. */
    static char *triggers[] = { "*", NULL };

    startBot(TB_CREATE_KV_STORE, argc, argv, TB_FLAGS_IGNORE_BAD_ARG,
             handle_request, cron_callback, triggers);
    return 0;
}
