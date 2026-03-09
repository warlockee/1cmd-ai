/*
 * terminal_display.c - Terminal text display for onecmd
 *
 * Contains send_html_message(), delete_terminal_messages(),
 * format_terminal_messages(), send_terminal_text().
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "types.h"
#include "bot.h"
#include "terminal_io.h"
#include "backend.h"
#include "format.h"
#include "sds.h"
#include "cJSON.h"
#include "state.h"

#define MAX_MSG_LEN 4085  /* 4096 - strlen("<pre></pre>") */
#define REFRESH_BTN "\xf0\x9f\x94\x84 Refresh"
#define REFRESH_DATA "refresh"

/* ============================================================================
 * HTML Message Sending
 * ========================================================================= */

/* Send a plain HTML message (no inline keyboard). Returns message_id or 0. */
int64_t send_html_message(int64_t target, sds text) {
    char *options[6];
    options[0] = "chat_id";
    options[1] = sdsfromlonglong(target);
    options[2] = "text";
    options[3] = text;
    options[4] = "parse_mode";
    options[5] = "HTML";
    int res;
    sds body = makeGETBotRequest("sendMessage", &res, options, 3);

    int64_t mid = 0;
    if (res && body) {
        cJSON *json = cJSON_Parse(body);
        cJSON *m = cJSON_Select(json, ".result.message_id:n");
        if (m) mid = (int64_t)m->valuedouble;
        cJSON_Delete(json);
    }

    sdsfree(body);
    sdsfree(options[1]);
    return mid;
}

/* ============================================================================
 * Terminal Message Tracking
 * ========================================================================= */

/* Delete all tracked terminal messages, then reset tracking. */
void delete_terminal_messages(int64_t chat_id) {
    for (int i = TrackedMsgCount - 1; i >= 0; i--) {
        char *options[4];
        options[0] = "chat_id";
        options[1] = sdsfromlonglong(chat_id);
        options[2] = "message_id";
        options[3] = sdsfromlonglong(TrackedMsgIds[i]);
        int res;
        sds body = makeGETBotRequest("deleteMessage", &res, options, 2);
        sdsfree(body);
        sdsfree(options[1]);
        sdsfree(options[3]);
    }
    TrackedMsgCount = 0;
}

/* ============================================================================
 * Terminal Message Formatting
 * ========================================================================= */

/* Format terminal text into one or more HTML <pre> messages.
 * When ONECMD_SPLIT_MESSAGES is enabled, splits on line boundaries when
 * content exceeds Telegram's 4096 char limit. Otherwise truncates to fit
 * a single message (keeping the tail end of the output).
 * Caller must sdsfree each element and xfree the array. */
sds *format_terminal_messages(sds raw, int *count) {
    int visible_lines = get_visible_lines();
    const char *tail = last_n_lines(raw, visible_lines);
    sds escaped = html_escape(tail);

    sds *msgs = NULL;
    int n = 0;
    int split = get_split_messages();

    if (!split) {
        /* Truncate mode: keep the tail that fits in one message. */
        if (sdslen(escaped) > MAX_MSG_LEN) {
            /* Find a newline near the cut point to avoid breaking a line. */
            const char *start = escaped + sdslen(escaped) - MAX_MSG_LEN;
            const char *nl = strchr(start, '\n');
            if (nl && (size_t)(nl - escaped) < sdslen(escaped))
                start = nl + 1;
            sds trimmed = sdsnew(start);
            sdsfree(escaped);
            escaped = trimmed;
        }
        msgs = xrealloc(msgs, sizeof(sds) * 1);
        msgs[n++] = sdscatprintf(sdsempty(), "<pre>%s</pre>", escaped);
    } else {
        /* Split mode: break into multiple messages. */
        while (sdslen(escaped) > 0) {
            if (sdslen(escaped) <= MAX_MSG_LEN) {
                msgs = xrealloc(msgs, sizeof(sds) * (n + 1));
                msgs[n++] = sdscatprintf(sdsempty(), "<pre>%s</pre>", escaped);
                break;
            }

            /* Find last newline within MAX_MSG_LEN to split on a line boundary. */
            char *cut = NULL;
            for (size_t i = MAX_MSG_LEN; i > 0; i--) {
                if (escaped[i - 1] == '\n') {
                    cut = escaped + i - 1;
                    break;
                }
            }

            if (!cut) {
                /* No newline found; hard-cut at MAX_MSG_LEN. */
                sds chunk = sdsnewlen(escaped, MAX_MSG_LEN);
                msgs = xrealloc(msgs, sizeof(sds) * (n + 1));
                msgs[n++] = sdscatprintf(sdsempty(), "<pre>%s</pre>", chunk);
                sdsfree(chunk);
                sdsrange(escaped, MAX_MSG_LEN, -1);
            } else {
                size_t chunk_len = cut - escaped;
                sds chunk = sdsnewlen(escaped, chunk_len);
                msgs = xrealloc(msgs, sizeof(sds) * (n + 1));
                msgs[n++] = sdscatprintf(sdsempty(), "<pre>%s</pre>", chunk);
                sdsfree(chunk);
                sdsrange(escaped, chunk_len + 1, -1); /* skip past newline */
            }
        }
    }

    sdsfree(escaped);

    if (n == 0) {
        msgs = xrealloc(msgs, sizeof(sds));
        msgs[0] = sdsnew("<pre></pre>");
        n = 1;
    }

    *count = n;
    return msgs;
}

/* ============================================================================
 * Terminal Text Display
 * ========================================================================= */

/* Send terminal text with refresh button (splits into multiple messages if needed).
 * Deletes previously tracked messages first to create a "live terminal view". */
void send_terminal_text(int64_t chat_id) {
    sds raw = backend_capture_text();
    if (!raw) {
        botSendMessage(chat_id, "Could not read terminal text.", 0);
        return;
    }

    delete_terminal_messages(chat_id);

    int count;
    sds *msgs = format_terminal_messages(raw, &count);
    sdsfree(raw);

    for (int i = 0; i < count - 1; i++) {
        int64_t mid = send_html_message(chat_id, msgs[i]);
        if (mid && TrackedMsgCount < MAX_TRACKED_MSGS)
            TrackedMsgIds[TrackedMsgCount++] = mid;
        sdsfree(msgs[i]);
    }

    int64_t last_mid = 0;
    botSendMessageWithKeyboard(chat_id, msgs[count - 1], "HTML",
                               REFRESH_BTN, REFRESH_DATA, &last_mid);
    if (last_mid && TrackedMsgCount < MAX_TRACKED_MSGS)
        TrackedMsgIds[TrackedMsgCount++] = last_mid;

    sdsfree(msgs[count - 1]);
    xfree(msgs);
}
