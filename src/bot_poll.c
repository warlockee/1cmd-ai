/* Copyright (c) 2023, Salvatore Sanfilippo <antirez at gmail dot com>
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 *
 *   * Redistributions of source code must retain the above copyright notice,
 *     this list of conditions and the following disclaimer.
 *   * Redistributions in binary form must reproduce the above copyright
 *     notice, this list of conditions and the following disclaimer in the
 *     documentation and/or other materials provided with the distribution.
 *   * Neither the name of Redis nor the names of its contributors may be used
 *     to endorse or promote products derived from this software without
 *     specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
 * AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
 * LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
 * CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
 * SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 * INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
 * CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
 * ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 * POSSIBILITY OF SUCH DAMAGE.
 */

/*
 * bot_poll.c - Polling loop, initialization, and global state for onecmd
 *
 * Contains the main bot event loop (botMain), the update polling function
 * (botProcessUpdates), bot initialization (startBot), database init/close,
 * and all global state definitions for the onecmd bot framework.
 */

/* Adding these for portability */
#define _BSD_SOURCE
#if defined(__linux__)
#define _GNU_SOURCE
#define _DEFAULT_SOURCE
#endif

#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdarg.h>
#include <pthread.h>
#include <ctype.h>
#include <unistd.h>
#include <math.h>
#include <time.h>

#include <curl/curl.h>
#include <sqlite3.h>

#include "types.h"
#include "bot.h"
#include "sds.h"
#include "cJSON.h"
#include "sqlite_wrap.h"
#include "state.h"

/* Thread local and atomic state. */
_Thread_local sqlite3 *DbHandle = NULL; /* Per-thread sqlite handle. */

/* The bot global state (type defined in bot.h). */
OneCmdBot Bot;

/* Global stats. Sometimes we access such stats from threads without caring
 * about race conditions, since they in practice are very unlikely to happen
 * in most archs with this data types, and even so we don't care.
 * This stuff is reported by the bot when the $$ info command is used. */
struct {
    time_t start_time;      /* Unix time the bot was started. */
    uint64_t queries;       /* Number of queries received. */
} botStats;

/* =============================================================================
 * Database abstraction
 * ===========================================================================*/

/* Create the SQLite tables if needed (if createdb is true), and return
 * the SQLite database handle. Return NULL on error. */
sqlite3 *dbInit(char *createdb_query) {
    sqlite3 *db;
    int rt = sqlite3_open(Bot.dbfile, &db);
    if (rt != SQLITE_OK) {
        fprintf(stderr, "Cannot open database: %s\n", sqlite3_errmsg(db));
        sqlite3_close(db);
        return NULL;
    }

    if (createdb_query) {
        char *errmsg;
        int rc = sqlite3_exec(db, createdb_query, 0, 0, &errmsg);
        if (rc != SQLITE_OK) {
            fprintf(stderr, "SQL error [%d]: %s\n", rc, errmsg);
            sqlite3_free(errmsg);
            sqlite3_close(db);
            return NULL;
        }
    }
    return db;
}

/* Should be called every time a thread exits, so that if the thread has
 * an SQLite thread-local handle, it gets closed. */
void dbClose(void) {
    if (DbHandle) sqlite3_close(DbHandle);
    DbHandle = NULL;
}

/* =============================================================================
 * Bot requests handling
 * ========================================================================== */

/* Request handling thread entry point. */
void *botHandleRequest(void *arg) {
    DbHandle = dbInit(NULL);
    BotRequest *br = arg;

    /* Parse the request as a command composed of arguments. */
    br->argv = sdssplitargs(br->request,&br->argc);
    Bot.req_callback(DbHandle,br);
    freeBotRequest(br);
    dbClose();
    return NULL;
}

/* Get the updates from the Telegram API, process them, and return the
 * ID of the highest processed update.
 *
 * The offset is the last ID already processed, the timeout is the number
 * of seconds to wait in long polling in case no request is immediately
 * available. */
int64_t botProcessUpdates(int64_t offset, int timeout) {
    char *options[6];
    int res;

    options[0] = "offset";
    options[1] = sdsfromlonglong(offset+1);
    options[2] = "timeout";
    options[3] = sdsfromlonglong(timeout);
    options[4] = "allowed_updates";
    options[5] = "[\"message\",\"callback_query\"]";
    sds body = makeGETBotRequest("getUpdates",&res,options,3);
    sdsfree(options[1]);
    sdsfree(options[3]);

    /* If two --debug options are provided, log the whole Telegram
     * reply here. */
    if (Bot.debug >= 2)
        printf("RECEIVED FROM TELEGRAM API:\n%s\n",body);

    /* Parse the JSON in order to extract the message info. */
    cJSON *json = cJSON_Parse(body);
    cJSON *result = cJSON_Select(json,".result:a");
    if (result == NULL) goto fmterr;
    /* Process the array of updates.
     * last_processed_id guards against duplicate processing when the same
     * update is returned twice (e.g. after a network error resets the offset).
     * It is persisted to SQLite so restarts also benefit. */
    static int64_t last_processed_id = -1;
    if (last_processed_id == -1) {
        sds stored = kvGet(DbHandle, "last_update_id");
        last_processed_id = stored ? strtoll(stored, NULL, 10) : 0;
        sdsfree(stored);
    }
    cJSON *update;
    cJSON_ArrayForEach(update,result) {
        cJSON *update_id = cJSON_Select(update,".update_id:n");
        if (update_id == NULL) continue;
        int64_t thisoff = (int64_t) update_id->valuedouble;
        if (thisoff > offset) offset = thisoff;

        /* Skip updates we already processed (guards against re-delivery
         * after network errors that cause getUpdates to return the old
         * offset). */
        if (thisoff <= last_processed_id) continue;

        /* Check for callback query (button press) first. */
        cJSON *callback = cJSON_Select(update,".callback_query");
        if (callback) {
            cJSON *cb_id = cJSON_Select(callback,".id:s");
            cJSON *cb_data = cJSON_Select(callback,".data:s");
            cJSON *cb_from = cJSON_Select(callback,".from.id:n");
            cJSON *cb_msg = cJSON_Select(callback,".message");
            if (cb_id && cb_data && cb_from && cb_msg) {
                cJSON *chatid = cJSON_Select(cb_msg,".chat.id:n");
                cJSON *msgid = cJSON_Select(cb_msg,".message_id:n");
                if (chatid && msgid) {
                    BotRequest *br = createBotRequest();
                    br->is_callback = 1;
                    br->callback_id = sdsnew(cb_id->valuestring);
                    br->callback_data = sdsnew(cb_data->valuestring);
                    br->from = (int64_t)cb_from->valuedouble;
                    br->target = (int64_t)chatid->valuedouble;
                    br->msg_id = (int64_t)msgid->valuedouble;
                    br->request = sdsnew(cb_data->valuestring);
                    br->type = TB_TYPE_PRIVATE;

                    botStats.queries++;
                    pthread_t tid;
                    if (pthread_create(&tid,NULL,botHandleRequest,br) == 0) {
                        pthread_detach(tid);
                    } else {
                        freeBotRequest(br);
                    }
                }
            }
            last_processed_id = thisoff;
            {
                char buf[32];
                snprintf(buf, sizeof(buf), "%lld", (long long)thisoff);
                kvSet(DbHandle, "last_update_id", buf, 0);
            }
            continue;
        }

        /* The actual message may be stored in .message or .channel_post
         * depending on the fact this is a private or group message,
         * or, instead, a channel post. */
        cJSON *msg = cJSON_Select(update,".message");
        if (!msg) msg = cJSON_Select(update,".channel_post");
        if (!msg) continue;

        cJSON *chatid = cJSON_Select(msg,".chat.id:n");
        if (chatid == NULL) continue;
        int64_t target = (int64_t) chatid->valuedouble;

        cJSON *fromid = cJSON_Select(msg,".from.id:n");
        int64_t from = fromid ? (int64_t) fromid->valuedouble : 0;

        cJSON *fromuser = cJSON_Select(msg,".from.username:s");
        char *from_username = fromuser ? fromuser->valuestring : "unknown";

        cJSON *msgid = cJSON_Select(msg,".message_id:n");
        int64_t message_id = msgid ? (int64_t) msgid->valuedouble : 0;

        cJSON *chattype = cJSON_Select(msg,".chat.type:s");
        char *ct = chattype->valuestring;
        int type = TB_TYPE_UNKNOWN;
        if (ct != NULL) {
            if (!strcmp(ct,"private")) type = TB_TYPE_PRIVATE;
            else if (!strcmp(ct,"group")) type = TB_TYPE_GROUP;
            else if (!strcmp(ct,"supergroup")) type = TB_TYPE_SUPERGROUP;
            else if (!strcmp(ct,"channel")) type = TB_TYPE_CHANNEL;
        }

        cJSON *date = cJSON_Select(msg,".date:n");
        if (date == NULL) continue;
        time_t timestamp = date->valuedouble;
        cJSON *text = cJSON_Select(msg,".text:s");
        /* Text may be NULL even if the message is valid but
         * is a voice message, image, ... .*/

        if (Bot.verbose) printf(".text (from: %lld, target: %lld): %s\n",
            (long long) from,
            (long long) target,
            text ? text->valuestring : "<no text field>");

        /* Sanity check the request before starting the thread:
         * validate that is a request that is really targeting our bot
         * list of "triggers". */
        if (text && type != TB_TYPE_PRIVATE && Bot.triggers) {
            char *s = text->valuestring;
            int j;
            for (j = 0; Bot.triggers[j]; j++) {
                if (strmatch(Bot.triggers[j], strlen(Bot.triggers[j]),
                    s, strlen(s), 1))
                {
                    break;
                }
            }
            if (Bot.triggers[j] == NULL) continue; // No match.
        }
        if (time(NULL)-timestamp > 60*5) continue; // Ignore stale messages

        /* At this point we are sure we are going to pass the request
         * to our callback. Prepare the request object. */
        sds request = sdsnew(text ? text->valuestring : "");
        BotRequest *br = createBotRequest();
        br->request = request;
        br->from_username = sdsnew(from_username);

        /* Check for files: voice, audio, or document. */
        cJSON *voice = cJSON_Select(msg,".voice.file_id:s");
        if (voice) {
            br->file_type = TB_FILE_TYPE_VOICE_OGG;
            br->file_id = sdsnew(voice->valuestring);
            cJSON *size = cJSON_Select(msg,".voice.file_size:n");
            br->file_size = size ? size->valuedouble : 0;
        }

        cJSON *audio = cJSON_Select(msg,".audio.file_id:s");
        if (audio && br->file_type == TB_FILE_TYPE_NONE) {
            br->file_type = TB_FILE_TYPE_AUDIO;
            br->file_id = sdsnew(audio->valuestring);
            cJSON *size = cJSON_Select(msg,".audio.file_size:n");
            cJSON *mime = cJSON_Select(msg,".audio.mime_type:s");
            cJSON *name = cJSON_Select(msg,".audio.file_name:s");
            br->file_size = size ? size->valuedouble : 0;
            br->file_mime = mime ? sdsnew(mime->valuestring) : NULL;
            br->file_name = name ? sdsnew(name->valuestring) : NULL;
        }

        cJSON *doc = cJSON_Select(msg,".document.file_id:s");
        if (doc && br->file_type == TB_FILE_TYPE_NONE) {
            br->file_type = TB_FILE_TYPE_DOCUMENT;
            br->file_id = sdsnew(doc->valuestring);
            cJSON *size = cJSON_Select(msg,".document.file_size:n");
            cJSON *mime = cJSON_Select(msg,".document.mime_type:s");
            cJSON *name = cJSON_Select(msg,".document.file_name:s");
            br->file_size = size ? size->valuedouble : 0;
            br->file_mime = mime ? sdsnew(mime->valuestring) : NULL;
            br->file_name = name ? sdsnew(name->valuestring) : NULL;
        }

        /* Parse entities, filling the mentions array. */
        cJSON *entities = cJSON_Select(msg,".entities[0]");
        while(entities) {
            cJSON *et = cJSON_Select(entities,".type:s");
            cJSON *offset = cJSON_Select(entities,".offset:n");
            cJSON *length = cJSON_Select(entities,".length:n");
            if (et && offset && length && !strcmp(et->valuestring,"mention")) {
                unsigned long off = offset->valuedouble;
                unsigned long len = length->valuedouble;
                /* Don't trust Telegram offsets inside our string. */
                if (off+len <= sdslen(br->request)) {
                    sds mention = sdsnewlen(br->request+off,len);
                    br->num_mentions++;
                    br->mentions = xrealloc(br->mentions,br->num_mentions * sizeof(sds));
                    br->mentions[br->num_mentions-1] = mention;
                    /* Is the user addressing the bot? Set the flag. */
                    if (Bot.username && !strcmp(Bot.username,mention+1))
                        br->bot_mentioned = 1;
                }
            }
            entities = entities->next;
        }

        br->type = type;
        br->from = from;
        br->target = target;
        br->msg_id = message_id;

        /* Spawn a thread that will handle the request. */
        botStats.queries++;
        pthread_t tid;
        if (pthread_create(&tid,NULL,botHandleRequest,br) != 0) {
            freeBotRequest(br);
            continue;
        }
        pthread_detach(tid);
        if (Bot.verbose)
            printf("Starting thread to serve: \"%s\"\n",br->request);

        last_processed_id = thisoff;
        {
            char buf[32];
            snprintf(buf, sizeof(buf), "%lld", (long long)thisoff);
            kvSet(DbHandle, "last_update_id", buf, 0);
        }

        /* It's up to the callback to free the bot request with
         * freeBotRequest(). */
    }

fmterr:
    cJSON_Delete(json);
    sdsfree(body);
    return offset;
}

/* =============================================================================
 * Bot main loop
 * ===========================================================================*/

/* This is the bot main loop: we get messages using getUpdates in blocking
 * mode, but with a timeout. Then we serve requests as needed, and every
 * time we unblock, we check for completed requests (by the thread that
 * handles Yahoo Finance API calls). */
void botMain(void) {
    /* Resume from last processed update to avoid re-processing on restart. */
    sds stored_id = kvGet(DbHandle, "last_update_id");
    int64_t nextid = stored_id ? strtoll(stored_id, NULL, 10) : -100;
    sdsfree(stored_id);
    int64_t previd;

    printf("Connecting to Telegram...\n");
    fflush(stdout);
    botGetUsername(); // Will cache Bot.username as side effect.
    if (Bot.username) {
        printf("Bot @%s is running. Waiting for messages.\n", Bot.username);
    } else {
        printf("Warning: could not fetch bot username. Continuing anyway.\n");
    }
    fflush(stdout);
    while(!ShutdownRequested) {
        previd = nextid;
        nextid = botProcessUpdates(nextid,1);
        /* We don't want to saturate all the CPU in a busy loop in case
         * the above call fails and returns immediately (for networking
         * errors for instance), so wait a bit at every cycle, but only
         * if we didn't made any progresses with the ID. */
        if (nextid == previd) usleep(100000);
        if (Bot.cron_callback) Bot.cron_callback(DbHandle);
    }
}

/* Check if a file named 'apikey.txt' exists, if so load the Telegram bot
 * API key from there. If the function is able to read the API key from
 * the file, as a side effect the global SDS string Bot.apikey is populated. */
void readApiKeyFromFile(void) {
    FILE *fp = fopen("apikey.txt","r");
    if (fp == NULL) return;
    char buf[1024];
    if (fgets(buf,sizeof(buf),fp) == NULL) {
        fclose(fp);
        return;
    }
    buf[sizeof(buf)-1] = '\0';
    fclose(fp);
    sdsfree(Bot.apikey);
    Bot.apikey = sdsnew(buf);
    Bot.apikey = sdstrim(Bot.apikey," \t\r\n");
}

void resetBotStats(void) {
    botStats.start_time = time(NULL);
    botStats.queries = 0;
}

int startBot(char *createdb_query, int argc, char **argv, int flags, TBRequestCallback req_callback, TBCronCallback cron_callback, char **triggers) {
    srand(time(NULL));

    Bot.debug = 0;
    Bot.verbose = 0;
    Bot.dbfile = "./mybot.sqlite";
    Bot.triggers = triggers;
    Bot.apikey = NULL;
    Bot.req_callback = req_callback;
    Bot.cron_callback = cron_callback;

    /* Parse options. */
    for (int j = 1; j < argc; j++) {
        int morearg = argc-j-1;
        if (!strcmp(argv[j],"--debug")) {
            Bot.debug++;
            Bot.verbose = 1;
        } else if (!strcmp(argv[j],"--verbose")) {
            Bot.verbose = 1;
        } else if (!strcmp(argv[j],"--apikey") && morearg) {
            Bot.apikey = sdsnew(argv[++j]);
        } else if (!strcmp(argv[j],"--dbfile") && morearg) {
            Bot.dbfile = argv[++j];
        } else if (!(flags & TB_FLAGS_IGNORE_BAD_ARG)) {
            printf(
            "Usage: %s [--apikey <apikey>] [--debug] [--verbose] "
            "[--dbfile <filename>]"
            "\n",argv[0]);
            exit(1);
        }
    }

    /* Initializations. Note that we don't redefine the SQLite allocator,
     * since SQLite errors are always handled by onecmd anyway. */
    curl_global_init(CURL_GLOBAL_DEFAULT);
    if (Bot.apikey == NULL) readApiKeyFromFile();
    if (Bot.apikey == NULL) {
        printf("Provide a bot API key via --apikey or storing a file named "
               "apikey.txt in the bot working directory.\n");
        exit(1);
    }
    resetBotStats();
    DbHandle = dbInit(createdb_query);
    if (DbHandle == NULL) exit(1);
    cJSON_Hooks jh = {.malloc_fn = xmalloc, .free_fn = xfree};
    cJSON_InitHooks(&jh);

    /* Enter the infinite loop handling the bot. */
    botMain();
    return 0;
}
