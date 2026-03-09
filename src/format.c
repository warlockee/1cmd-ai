/*
 * format.c - Message formatting for onecmd
 *
 * Contains markdown/HTML escaping, list/help message builders,
 * visible-line config, message splitting, and line-tail extraction.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>

#include "types.h"
#include "format.h"
#include "backend.h"
#include "sds.h"
#include "cJSON.h"

/* ============================================================================
 * Text Escaping
 * ========================================================================= */

/* Escape Markdown special characters in a string so that
 * botSendMessage() (which uses parse_mode=Markdown) won't choke
 * on user-controlled text like window titles. */
sds markdown_escape(const char *s) {
    sds out = sdsempty();
    for (; *s; s++) {
        if (*s == '_' || *s == '*' || *s == '`' || *s == '[')
            out = sdscatlen(out, "\\", 1);
        out = sdscatlen(out, s, 1);
    }
    return out;
}

/* Escape text for Telegram HTML parse mode. */
sds html_escape(const char *text) {
    sds out = sdsempty();
    for (const char *p = text; *p; p++) {
        switch (*p) {
            case '<': out = sdscat(out, "&lt;"); break;
            case '>': out = sdscat(out, "&gt;"); break;
            case '&': out = sdscat(out, "&amp;"); break;
            default:  out = sdscatlen(out, p, 1); break;
        }
    }
    return out;
}

/* ============================================================================
 * Terminal Aliases
 * ========================================================================= */

#define ALIASES_PATH ".onecmd/aliases.json"

/* Read aliases JSON file, returns parsed cJSON object or NULL. */
static cJSON *read_aliases_file(void) {
    FILE *f = fopen(ALIASES_PATH, "r");
    if (!f) return NULL;

    fseek(f, 0, SEEK_END);
    long len = ftell(f);
    fseek(f, 0, SEEK_SET);

    if (len <= 0) { fclose(f); return NULL; }

    char *buf = malloc(len + 1);
    if (!buf) { fclose(f); return NULL; }

    fread(buf, 1, len, f);
    buf[len] = '\0';
    fclose(f);

    cJSON *root = cJSON_Parse(buf);
    free(buf);
    return root;
}

/* Get alias for a terminal ID. Returns sds string or NULL. Caller frees. */
sds get_alias(const char *terminal_id) {
    cJSON *root = read_aliases_file();
    if (!root) return NULL;

    cJSON *item = cJSON_GetObjectItemCaseSensitive(root, terminal_id);
    sds result = NULL;
    if (cJSON_IsString(item) && item->valuestring[0]) {
        result = sdsnew(item->valuestring);
    }

    cJSON_Delete(root);
    return result;
}

/* Save an alias for a terminal ID. Creates .onecmd/ dir if needed. */
void save_alias(const char *terminal_id, const char *name) {
    mkdir(".onecmd", 0755);

    cJSON *root = read_aliases_file();
    if (!root) root = cJSON_CreateObject();

    /* Remove existing entry if present, then add new one. */
    cJSON_DeleteItemFromObjectCaseSensitive(root, terminal_id);
    cJSON_AddStringToObject(root, terminal_id, name);

    char *json = cJSON_Print(root);
    if (json) {
        FILE *f = fopen(ALIASES_PATH, "w");
        if (f) {
            fputs(json, f);
            fclose(f);
        }
        free(json);
    }

    cJSON_Delete(root);
}

/* ============================================================================
 * Message Builders
 * ========================================================================= */

/* Build the .list response. */
sds build_list_message(void) {
    backend_list();

    sds msg = sdsempty();
    if (TermCount == 0) {
        msg = sdscat(msg, "No terminal sessions found.");
        return msg;
    }

    msg = sdscat(msg, "Terminal windows:\n");
    for (int i = 0; i < TermCount; i++) {
        TermInfo *t = &TermList[i];
        sds ename = markdown_escape(t->name);
        sds etitle = markdown_escape(t->title);
        sds alias = get_alias(t->id);
        if (alias) {
            sds ealias = markdown_escape(alias);
            if (t->title[0]) {
                msg = sdscatprintf(msg, ".%d [%s] %s - %s\n",
                                   i + 1, ealias, ename, etitle);
            } else {
                msg = sdscatprintf(msg, ".%d [%s] %s\n",
                                   i + 1, ealias, ename);
            }
            sdsfree(ealias);
            sdsfree(alias);
        } else {
            if (t->title[0]) {
                msg = sdscatprintf(msg, ".%d %s - %s\n",
                                   i + 1, ename, etitle);
            } else {
                msg = sdscatprintf(msg, ".%d %s\n",
                                   i + 1, ename);
            }
        }
        sdsfree(ename);
        sdsfree(etitle);
    }
    return msg;
}

sds build_help_message(void) {
    return sdsnew(
        "Commands:\n"
        ".list - Show terminal windows\n"
        ".1 .2 ... - Connect to window\n"
        ".rename N name - Name a terminal\n"
        ".mgr - Toggle AI manager mode\n"
        ".exit - Leave manager mode\n"
        ".health - Manager health report\n"
        ".help - This help\n\n"
        "Once connected, text is sent as keystrokes.\n"
        "Newline is auto-added; end with `\xf0\x9f\x92\x9c` to suppress it.\n\n"
        "Modifiers (tap to copy, then paste + key):\n"
        "`\xe2\x9d\xa4\xef\xb8\x8f` Ctrl  `\xf0\x9f\x92\x99` Alt  "
        "`\xf0\x9f\x92\x9a` Cmd  `\xf0\x9f\x92\x9b` ESC  "
        "`\xf0\x9f\xa7\xa1` Enter\n\n"
        "Escape sequences: \\n=Enter \\t=Tab"
    );
}

/* ============================================================================
 * Visible Lines & Splitting Config
 * ========================================================================= */

/* Get visible lines from ONECMD_VISIBLE_LINES env var, defaulting to 40. */
int get_visible_lines(void) {
    const char *env = getenv("ONECMD_VISIBLE_LINES");
    if (env) {
        int v = atoi(env);
        if (v > 0) return v;
    }
    return 40;
}

/* Check if multi-message splitting is enabled (default: off = truncate). */
int get_split_messages(void) {
    const char *env = getenv("ONECMD_SPLIT_MESSAGES");
    if (env && (strcmp(env, "1") == 0 || strcasecmp(env, "true") == 0))
        return 1;
    return 0;
}

/* ============================================================================
 * Line Extraction
 * ========================================================================= */

/* Get the last N lines from text. Returns pointer into the string. */
const char *last_n_lines(const char *text, int n) {
    const char *end = text + strlen(text);
    const char *p = end;
    int count = 0;
    while (p > text) {
        p--;
        if (*p == '\n') {
            count++;
            if (count >= n) { p++; break; }
        }
    }
    return p;
}
