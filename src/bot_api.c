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
 * bot_api.c - Telegram Bot API layer for onecmd
 *
 * Higher-level functions that wrap the HTTP layer (bot_http.c) to interact
 * with the Telegram Bot API: sending messages, editing messages, downloading
 * files, and managing bot request objects.
 */

/* Adding these for portability */
#define _BSD_SOURCE
#if defined(__linux__)
#define _GNU_SOURCE
#define _DEFAULT_SOURCE
#endif

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <pthread.h>

#include <curl/curl.h>

#include "types.h"
#include "bot.h"
#include "sds.h"
#include "cJSON.h"

/* makeHTTPGETCallWriterFILE is declared in bot.h */

/* Make an HTTP request to the Telegram bot API, where 'req' is the specified
 * action name. This is a low level API that is used by other bot APIs
 * in order to do higher level work. 'resptr' works the same as in
 * makeHTTPGETCall(). */
sds makeGETBotRequest(const char *action, int *resptr, char **optlist, int numopt)
{
    sds url = sdsnew("https://api.telegram.org/bot");
    url = sdscat(url,Bot.apikey);
    url = sdscatlen(url,"/",1);
    url = sdscat(url,action);
    sds body = makeHTTPGETCallOpt(url,resptr,optlist,numopt);
    sdsfree(url);
    return body;
}

/* Answer a callback query to dismiss the "loading" state. */
int botAnswerCallbackQuery(const char *callback_id) {
    char *options[2];
    options[0] = "callback_query_id";
    options[1] = (char *)callback_id;

    int res;
    sds body = makeGETBotRequest("answerCallbackQuery", &res, options, 1);
    sdsfree(body);
    return res;
}

/* =============================================================================
 * Higher level Telegram bot API.
 * ===========================================================================*/

/* Return the bot username. */
char *botGetUsername(void) {
    int res;

    if (Bot.username) return Bot.username;
    sds body = makeGETBotRequest("getMe",&res,NULL,0);
    if (res == 0) return NULL;

    cJSON *json = cJSON_Parse(body), *username;
    username = cJSON_Select(json,".result.username:s");
    if (username) Bot.username = sdsnew(username->valuestring);
    sdsfree(body);
    cJSON_Delete(json);
    return Bot.username;
}

/* Send a message to the specified channel, optionally as a reply to a
 * specific message (if reply_to is non zero).
 * Return 1 on success, 0 on error. */
int botSendMessageAndGetInfo(int64_t target, sds text, int64_t reply_to, int64_t *chat_id, int64_t *message_id) {
    char *options[10];
    int optlen = 4;
    options[0] = "chat_id";
    options[1] = sdsfromlonglong(target);
    options[2] = "text";
    options[3] = text;
    options[4] = "parse_mode";
    options[5] = "Markdown";
    options[6] = "disable_web_page_preview";
    options[7] = "true";
    if (reply_to) {
        optlen++;
        options[8] = "reply_to_message_id";
        options[9] = sdsfromlonglong(reply_to);
    } else {
        options[9] = NULL; /* So we can sdsfree it later without problems. */
    }

    int res;
    sds body = makeGETBotRequest("sendMessage",&res,options,optlen);

    if (chat_id || message_id) {
        cJSON *json = cJSON_Parse(body);
        cJSON *field;
        field = cJSON_Select(json,".result.message_id:n");
        if (field && message_id) *message_id = (int64_t) field->valuedouble;
        field = cJSON_Select(json,".result.chat.id:n");
        if (field && chat_id) *chat_id = (int64_t) field->valuedouble;
        cJSON_Delete(json);
    }

    sdsfree(body);
    sdsfree(options[1]);
    sdsfree(options[9]);
    return res;
}

/* Like botSendMessageWithInfo() but without returning by reference
 * the chat and message IDs that are only useful if you want to
 * edit the message later.
 * Return 1 on success, 0 on error. */
int botSendMessage(int64_t target, sds text, int64_t reply_to) {
    return botSendMessageAndGetInfo(target,text,reply_to,NULL,NULL);
}

/* Fire-and-forget message send in a detached thread. */
struct async_msg {
    int64_t target;
    int64_t reply_to;
    char text[];
};

static void *async_send_thread(void *arg) {
    struct async_msg *m = arg;
    sds s = sdsnew(m->text);
    botSendMessage(m->target, s, m->reply_to);
    sdsfree(s);
    free(m);
    return NULL;
}

void botSendMessageAsync(int64_t target, const char *text, int64_t reply_to) {
    size_t len = strlen(text) + 1;
    struct async_msg *m = malloc(sizeof(*m) + len);
    m->target = target;
    m->reply_to = reply_to;
    memcpy(m->text, text, len);
    pthread_t t;
    if (pthread_create(&t, NULL, async_send_thread, m) == 0) {
        pthread_detach(t);
    } else {
        /* Fallback: send synchronously. */
        sds s = sdsnew(text);
        botSendMessage(target, s, reply_to);
        sdsfree(s);
        free(m);
    }
}

/* Send a text message with an inline keyboard button. Returns message_id
 * via msg_id if not NULL. Return 1 on success, 0 on error. */
int botSendMessageWithKeyboard(int64_t target, sds text, const char *parse_mode, const char *btn_text, const char *btn_data, int64_t *msg_id) {
    sds keyboard = sdscatprintf(sdsempty(),
        "{\"inline_keyboard\":[[{\"text\":\"%s\",\"callback_data\":\"%s\"}]]}",
        btn_text, btn_data);

    char *options[10];
    int optlen = 4;
    options[0] = "chat_id";
    options[1] = sdsfromlonglong(target);
    options[2] = "text";
    options[3] = text;
    options[4] = "parse_mode";
    options[5] = (char*)parse_mode;
    options[6] = "reply_markup";
    options[7] = keyboard;

    int res;
    sds body = makeGETBotRequest("sendMessage",&res,options,optlen);

    if (msg_id && res) {
        cJSON *json = cJSON_Parse(body);
        cJSON *mid = cJSON_Select(json,".result.message_id:n");
        if (mid) *msg_id = (int64_t)mid->valuedouble;
        cJSON_Delete(json);
    }

    sdsfree(body);
    sdsfree(options[1]);
    sdsfree(keyboard);
    return res;
}

/* Edit an existing text message, preserving the inline keyboard button.
 * Return 1 on success, 0 on error. */
int botEditMessageTextWithKeyboard(int64_t chat_id, int64_t message_id, sds text, const char *parse_mode, const char *btn_text, const char *btn_data) {
    sds keyboard = sdscatprintf(sdsempty(),
        "{\"inline_keyboard\":[[{\"text\":\"%s\",\"callback_data\":\"%s\"}]]}",
        btn_text, btn_data);

    char *options[12];
    int optlen = 5;
    options[0] = "chat_id";
    options[1] = sdsfromlonglong(chat_id);
    options[2] = "message_id";
    options[3] = sdsfromlonglong(message_id);
    options[4] = "text";
    options[5] = text;
    options[6] = "parse_mode";
    options[7] = (char*)parse_mode;
    options[8] = "reply_markup";
    options[9] = keyboard;

    int res;
    sds body = makeGETBotRequest("editMessageText",&res,options,optlen);
    sdsfree(body);
    sdsfree(options[1]);
    sdsfree(options[3]);
    sdsfree(keyboard);
    return res;
}

/* Send a message to the specified channel, optionally as a reply to a
 * specific message (if reply_to is non zero).
 * Return 1 on success, 0 on error. */
int botEditMessageText(int64_t chat_id, int message_id, sds text) {
    char *options[10];
    int optlen = 5;
    options[0] = "chat_id";
    options[1] = sdsfromlonglong(chat_id);
    options[2] = "message_id";
    options[3] = sdsfromlonglong(message_id);
    options[4] = "text";
    options[5] = text;
    options[6] = "parse_mode";
    options[7] = "Markdown";
    options[8] = "disable_web_page_preview";
    options[9] = "true";

    int res;
    sds body = makeGETBotRequest("editMessageText",&res,options,optlen);
    sdsfree(body);
    sdsfree(options[1]);
    sdsfree(options[3]);
    return res;
}

/* This function should be called from the bot implementation callback.
 * If the bot request has a file (the user can see that by inspecting
 * the br->file_type field), then this function will attempt to download
 * the file from Telegram and store it in the current working directory
 * with the name 'br->file_id'.
 *
 * On success 1 is returned, otherwise 0.
 * When the function returns successfully, the caller can access
 * a file named 'br->file_id'. */
int botGetFile(BotRequest *br, const char *target_filename) {
    /* 1. Get the file information and path. */
    char *options[2];
    options[0] = "file_id";
    options[1] = br->file_id;

    int res;
    sds body = makeGETBotRequest("getFile",&res,options,1);
    if (res == 0) {
        sdsfree(body);
        return 0; // Error.
    }

    cJSON *json = cJSON_Parse(body);
    cJSON *result = cJSON_Select(json,".result.file_path:s");
    char *file_path = result ? result->valuestring : NULL;
    sdsfree(body);
    if (!file_path) {
        cJSON_Delete(json);
        return 0;
    }

    /* 2. Get the file content. */
    CURL* curl = curl_easy_init();
    if (!curl) {
        cJSON_Delete(json);
        return 0;
    }

    /* We need to open a file for writing. We will be
     * using the curl callback in order to append to the
     * file. */
    const char *filename = target_filename ? target_filename : br->file_id;
    FILE *fp = fopen(filename, "w");
    if (fp == NULL) {
        cJSON_Delete(json);
        curl_easy_cleanup(curl);
        return 0;
    }

    char url[1024];
    snprintf(url, sizeof(url),
        "https://api.telegram.org/file/bot%s/%s", Bot.apikey, file_path);
    cJSON_Delete(json);
    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, makeHTTPGETCallWriterFILE);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &fp);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 1L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 15);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 15);

    /* Perform the request and cleanup. */
    int retval = curl_easy_perform(curl) == CURLE_OK ? 1 : 0;
    curl_easy_cleanup(curl);
    fclose(fp);
    /* Best effort removal of incomplete file. */
    if (retval == 0) unlink(filename);
    return retval;
}

/* Free the bot request and associated data. */
void freeBotRequest(BotRequest *br) {
    sdsfreesplitres(br->argv,br->argc);
    sdsfree(br->request);
    sdsfree(br->file_id);
    sdsfree(br->file_name);
    sdsfree(br->file_mime);
    sdsfree(br->from_username);
    sdsfree(br->callback_id);
    sdsfree(br->callback_data);
    if (br->mentions) {
        for (int j = 0; j < br->num_mentions; j++) sdsfree(br->mentions[j]);
        free(br->mentions);
    }
    free(br);
}

/* Create a bot request object and return it to the caller. */
BotRequest *createBotRequest(void) {
    BotRequest *br = malloc(sizeof(*br));
    br->request = NULL;
    br->argc = 0;
    br->argv = NULL;
    br->from = 0;
    br->from_username = NULL;
    br->target = 0;
    br->msg_id = 0;
    br->file_id = NULL;
    br->file_name = NULL;
    br->file_mime = NULL;
    br->file_size = 0;
    br->type = TB_TYPE_UNKNOWN;
    br->file_type = TB_FILE_TYPE_NONE;
    br->bot_mentioned = 0;
    br->mentions = NULL;
    br->num_mentions = 0;
    br->is_callback = 0;
    br->callback_id = NULL;
    br->callback_data = NULL;
    return br;
}
