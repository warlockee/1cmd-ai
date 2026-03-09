/*
 * types.h - Core type definitions and constants for onecmd
 *
 * Calling spec:
 *   Inputs:  None (pure declarations)
 *   Outputs: None
 *   Side effects: None — header-only, no code generation
 *
 * This file contains ONLY struct/type definitions, constants, and global
 * state externs. No function declarations live here.
 */

#ifndef ONECMD_TYPES_H
#define ONECMD_TYPES_H

#include <stdint.h>
#include <sys/types.h>
#include <sqlite3.h>

#include "sds.h"

#ifndef UNUSED
#define UNUSED(V) ((void) V)
#endif

/* ---- Bot flags --------------------------------------------------------- */

#define TB_FLAGS_NONE           0
#define TB_FLAGS_IGNORE_BAD_ARG (1<<0)

/* ---- Request type constants -------------------------------------------- */

#define TB_TYPE_UNKNOWN    0
#define TB_TYPE_PRIVATE    1
#define TB_TYPE_GROUP      2
#define TB_TYPE_SUPERGROUP 3
#define TB_TYPE_CHANNEL    4

/* ---- File type constants ----------------------------------------------- */

#define TB_FILE_TYPE_NONE      0
#define TB_FILE_TYPE_VOICE_OGG 1
#define TB_FILE_TYPE_AUDIO     2
#define TB_FILE_TYPE_DOCUMENT  3

/* ---- Terminal session info --------------------------------------------- */

typedef struct {
    char id[128];         /* macOS: window_id as string, tmux: pane_id */
    pid_t pid;            /* process PID */
    char name[128];       /* macOS: app name, tmux: session:window.pane */
    char title[256];      /* window/pane title or current command */
} TermInfo;

/* ---- Bot request ------------------------------------------------------- */

typedef struct BotRequest {
    int type;             /* TB_TYPE_PRIVATE, ... */
    sds request;          /* The request string. */
    int64_t from;         /* ID of user sending the message. */
    sds from_username;    /* Username of the user sending the message. */
    int64_t target;       /* Target channel/user where to reply. */
    int64_t msg_id;       /* Message ID. */
    sds *argv;            /* Request split to single words. */
    int argc;             /* Number of words. */
    int file_type;        /* TB_FILE_TYPE_* */
    sds file_id;          /* File ID if a file is present. */
    sds file_name;        /* Original file name, if available. */
    sds file_mime;        /* MIME type, if available. */
    int64_t file_size;    /* Size of the file. */
    int bot_mentioned;    /* True if the bot was explicitly mentioned. */
    sds *mentions;        /* List of mentioned usernames (NULL if none). */
    int num_mentions;     /* Number of elements in 'mentions' array. */
    int is_callback;      /* True if this is a callback query (button press). */
    sds callback_id;      /* Callback query ID for answering. */
    sds callback_data;    /* Callback data from button. */
} BotRequest;

/* ---- Callback types ---------------------------------------------------- */

typedef void (*TBRequestCallback)(sqlite3 *dbhandle, BotRequest *br);
typedef void (*TBCronCallback)(sqlite3 *dbhandle);

/* ---- KV store schema --------------------------------------------------- */

#define TB_CREATE_KV_STORE \
    "CREATE TABLE IF NOT EXISTS KeyValue(expire INT, " \
                                        "key TEXT, " \
                                        "value BLOB);" \
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_kv_key ON KeyValue(key);" \
    "CREATE INDEX IF NOT EXISTS idx_ex_key ON KeyValue(expire);"

/* ---- Global state (defined in main.c) ---------------------------------- */

extern TermInfo *TermList;
extern int TermCount;

extern int Connected;
extern char ConnectedId[128];
extern pid_t ConnectedPid;
extern char ConnectedName[128];
extern char ConnectedTitle[256];

extern int DangerMode;

#endif /* ONECMD_TYPES_H */
