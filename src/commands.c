/*
 * commands.c - Command handlers for onecmd Telegram bot
 *
 * Contains handle_request() (the big command dispatch), cron_callback(),
 * disconnect(), and OTP authentication flow within handle_request.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include <ctype.h>
#include <pthread.h>
#include <time.h>

#include "types.h"
#include "bot.h"
#include "backend.h"
#include "commands.h"
#include "format.h"
#include "terminal_io.h"
#include "manager_ipc.h"
#include "totp.h"
#include "emoji.h"
#include "sqlite_wrap.h"
#include "state.h"

#define OWNER_KEY "owner_id"
#define REFRESH_BTN "\xf0\x9f\x94\x84 Refresh"
#define REFRESH_DATA "refresh"

/* ============================================================================
 * Connection Management
 * ========================================================================= */

/* Disconnect from current terminal session. */
void disconnect(void) {
    Connected = 0;
    ConnectedId[0] = '\0';
    ConnectedPid = 0;
    ConnectedName[0] = '\0';
    ConnectedTitle[0] = '\0';
    TrackedMsgCount = 0;
}

/* ============================================================================
 * Bot Command Handlers
 * ========================================================================= */

void handle_request(sqlite3 *db, BotRequest *br) {
    pthread_mutex_lock(&RequestLock);

    /* Check owner. First user to message becomes owner. */
    sds owner_str = kvGet(db, OWNER_KEY);
    int64_t owner_id = 0;

    if (owner_str) {
        owner_id = strtoll(owner_str, NULL, 10);
        sdsfree(owner_str);
    }

    if (owner_id == 0) {
        /* Register first user as owner. */
        char buf[32];
        snprintf(buf, sizeof(buf), "%lld", (long long)br->from);
        kvSet(db, OWNER_KEY, buf, 0);
        owner_id = br->from;
        printf("Registered owner: %lld (%s)\n", (long long)owner_id, br->from_username);
        botSendMessage(br->target,
            "Welcome to onecmd! You are now the owner.\n\n"
            "`.list` — show terminals\n"
            "`.mgr` — AI manager mode\n"
            "`.help` — all commands", 0);
        goto done;
    }

    if (br->from != owner_id) {
        printf("Ignoring message from non-owner %lld\n", (long long)br->from);
        goto done;
    }

    /* TOTP authentication check (applies to both messages and callbacks). */
    if (!WeakSecurity) {
        if (!Authenticated || time(NULL) - LastActivity > OtpTimeout) {
            Authenticated = 0;
            if (br->is_callback) {
                botAnswerCallbackQuery(br->callback_id);
                goto done;
            }
            char *req = br->request;
            /* Check if message is a 6-digit OTP code. */
            int is_otp = (strlen(req) == 6);
            for (int i = 0; is_otp && i < 6; i++) {
                if (!isdigit((unsigned char)req[i])) is_otp = 0;
            }
            if (is_otp && totp_verify(db, req)) {
                Authenticated = 1;
                LastActivity = time(NULL);
                botSendMessage(br->target, "Authenticated.", 0);
            } else {
                botSendMessage(br->target, "Enter OTP code.", 0);
            }
            goto done;
        }
        LastActivity = time(NULL);
    }

    /* Handle callback query (button press). */
    if (br->is_callback) {
        botAnswerCallbackQuery(br->callback_id);
        if (strcmp(br->callback_data, REFRESH_DATA) == 0 && Connected) {
            send_terminal_text(br->target);
        }
        goto done;
    }

    char *req = br->request;

    /* Handle .mgr command: toggle manager mode. */
    if (strcasecmp(req, ".mgr") == 0) {
        if (!MgrMode) {
            if (MgrPath[0] == '\0') {
                botSendMessage(br->target,
                    "Manager not configured. No LLM API key set.", 0);
                goto done;
            }
            if (mgr_start() != 0) {
                botSendMessage(br->target,
                    "Failed to start manager.", 0);
                goto done;
            }
            MgrMode = 1;
            botSendMessage(br->target,
                "\xf0\x9f\xa4\x96 Manager mode on.\n"
                "Dot commands still work: .list .1 .exit", 0);
        } else {
            MgrMode = 0;
            botSendMessage(br->target, "Manager mode off.", 0);
        }
        goto done;
    }

    /* Handle .exit command: leave manager mode. */
    if (strcasecmp(req, ".exit") == 0 && MgrMode) {
        MgrMode = 0;
        botSendMessage(br->target, "Manager mode off.", 0);
        goto done;
    }

    /* In manager mode, route non-dot messages to mgr.
     * Empty/whitespace-only messages are ignored to prevent
     * accidental newlines from "quick enter" on mobile. */
    if (MgrMode && req[0] != '.') {
        /* Skip empty/whitespace messages. */
        const char *p = req;
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
        if (*p == '\0') goto done;

        if (mgr_send(br->target, req) == 0) {
            botSendMessageAsync(br->target, "\xf0\x9f\xa4\x96 ...", 0);
        } else {
            botSendMessage(br->target,
                "Manager not available. Try .mgr to reconnect.", 0);
            MgrMode = 0;
        }
        goto done;
    }

    /* Handle .list command. */
    if (strcasecmp(req, ".list") == 0) {
        disconnect();
        sds msg = build_list_message();
        botSendMessage(br->target, msg, 0);
        sdsfree(msg);
        goto done;
    }

    /* Handle .health command: query manager for health report. */
    if (strcasecmp(req, ".health") == 0) {
        if (MgrPath[0] == '\0') {
            botSendMessage(br->target,
                "Manager not configured. No LLM API key set.", 0);
            goto done;
        }
        if (mgr_start() != 0) {
            botSendMessage(br->target, "Failed to start manager.", 0);
            goto done;
        }
        mgr_send(br->target, ".health");
        goto done;
    }

    /* Handle .help command. */
    if (strcasecmp(req, ".help") == 0) {
        sds msg = build_help_message();
        botSendMessage(br->target, msg, 0);
        sdsfree(msg);
        goto done;
    }

    /* Handle .otptimeout command. */
    if (strncasecmp(req, ".otptimeout", 11) == 0) {
        char *arg = req + 11;
        while (*arg == ' ') arg++;
        int secs = atoi(arg);
        if (secs < 30) secs = 30;
        if (secs > 28800) secs = 28800;
        OtpTimeout = secs;
        char buf[64];
        snprintf(buf, sizeof(buf), "%d", secs);
        kvSet(db, "otp_timeout", buf, 0);
        sds msg = sdscatprintf(sdsempty(), "OTP timeout set to %d seconds.", secs);
        botSendMessage(br->target, msg, 0);
        sdsfree(msg);
        goto done;
    }

    /* Handle .N to connect to terminal session N. */
    if (req[0] == '.' && isdigit(req[1])) {
        int n = atoi(req + 1);
        backend_list();

        if (n < 1 || n > TermCount) {
            botSendMessage(br->target, "Invalid window number.", 0);
            goto done;
        }

        /* Connecting to a terminal exits manager mode. */
        if (MgrMode) {
            MgrMode = 0;
            botSendMessage(br->target, "Manager mode off. 1-on-1 mode on.", 0);
        }

        /* Store connection info directly. */
        TermInfo *t = &TermList[n - 1];
        Connected = 1;
        strncpy(ConnectedId, t->id, sizeof(ConnectedId) - 1);
        ConnectedId[sizeof(ConnectedId) - 1] = '\0';
        ConnectedPid = t->pid;
        strncpy(ConnectedName, t->name, sizeof(ConnectedName) - 1);
        ConnectedName[sizeof(ConnectedName) - 1] = '\0';
        strncpy(ConnectedTitle, t->title, sizeof(ConnectedTitle) - 1);
        ConnectedTitle[sizeof(ConnectedTitle) - 1] = '\0';

        sds msg = sdsnew("Connected to ");
        msg = sdscat(msg, ConnectedName);
        if (ConnectedTitle[0]) {
            msg = sdscat(msg, " - ");
            msg = sdscat(msg, ConnectedTitle);
        }
        botSendMessage(br->target, msg, 0);
        sdsfree(msg);

        /* Send terminal text. */
        send_terminal_text(br->target);
        goto done;
    }

    /* Not a command - send as keystrokes if connected. */
    if (!Connected) {
        sds msg = build_list_message();
        botSendMessage(br->target, msg, 0);
        sdsfree(msg);
        goto done;
    }

    /* Check terminal session still exists. */
    if (!backend_connected()) {
        disconnect();
        sds msg = sdsnew("Window closed.\n\n");
        sds list = build_list_message();
        msg = sdscatsds(msg, list);
        sdsfree(list);
        botSendMessage(br->target, msg, 0);
        sdsfree(msg);
        goto done;
    }

    /* Send keystrokes. */
    backend_send_keys(req);

    /* Wait a bit for the terminal to react, then re-check the session
     * (keystrokes may switch panes/tabs, changing the active ID). */
    usleep(500000);
    backend_connected();
    send_terminal_text(br->target);

done:
    pthread_mutex_unlock(&RequestLock);
}

void cron_callback(sqlite3 *db) {
    UNUSED(db);
}
