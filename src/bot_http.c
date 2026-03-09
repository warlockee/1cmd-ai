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
 * bot_http.c - HTTP/Curl layer for onecmd
 *
 * Part of the onecmd bot framework. Contains the low-level HTTP interface
 * used by the Telegram Bot API layer (bot_api.c).
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

#include <curl/curl.h>

#include "bot.h"
#include "sds.h"
#include "cJSON.h"


/* ============================================================================
 * HTTP interface abstraction
 * ==========================================================================*/

/* The callback concatenating data arriving from CURL http requests into
 * a target SDS string. */
size_t makeHTTPGETCallWriterSDS(char *ptr, size_t size, size_t nmemb, void *userdata)
{
    UNUSED(size);
    sds *body = userdata;
    *body = sdscatlen(*body,ptr,nmemb);
    return nmemb;
}

/* The callback writing the CURL reply to a file. */
size_t makeHTTPGETCallWriterFILE(char *ptr, size_t size, size_t nmemb, void *userdata)
{
    UNUSED(size);
    FILE **fp = userdata;
    return fwrite(ptr,1,nmemb,*fp);
}


/* Request the specified URL in a blocking way, returns the content (or
 * error string) as an SDS string. If 'resptr' is not NULL, the integer
 * will be set, by reference, to 1 or 0 to indicate success or error.
 * The returned SDS string must be freed by the caller both in case of
 * error and success. */
sds makeHTTPGETCall(const char *url, int *resptr) {
    if (Bot.debug) printf("HTTP GET %s\n", url);
    CURL* curl;
    CURLcode res;
    sds body = sdsempty();

    curl = curl_easy_init();
    if (curl) {
        curl_easy_setopt(curl, CURLOPT_URL, url);
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, makeHTTPGETCallWriterSDS);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 1L);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT, 15);
        curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 15);

        /* Perform the request, res will get the return code */
        res = curl_easy_perform(curl);
        if (resptr) *resptr = res == CURLE_OK ? 1 : 0;

        /* Check for errors */
        if (res != CURLE_OK) {
            const char *errstr = curl_easy_strerror(res);
            body = sdscat(body,errstr);
        } else {
            /* Return 0 if the request worked but returned a 500 code. */
            long code;
            curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &code);
            if ((code == 500 || code == 400) && resptr) *resptr = 0;
        }

        /* always cleanup */
        curl_easy_cleanup(curl);
    }
    return body;
}

/* Like makeHTTPGETCall(), but the list of options will be concatenated to
 * the URL as a query string, and URL encoded as needed.
 * The option list array should contain optnum*2 strings, alternating
 * option names and values. */
sds makeHTTPGETCallOpt(const char *url, int *resptr, char **optlist, int optnum) {
    sds fullurl = sdsnew(url);
    if (optnum) fullurl = sdscatlen(fullurl,"?",1);
    CURL *curl = curl_easy_init();
    for (int j = 0; j < optnum; j++) {
        if (j > 0) fullurl = sdscatlen(fullurl,"&",1);
        fullurl = sdscat(fullurl,optlist[j*2]);
        fullurl = sdscatlen(fullurl,"=",1);
        char *escaped = curl_easy_escape(curl,
            optlist[j*2+1],strlen(optlist[j*2+1]));
        fullurl = sdscat(fullurl,escaped);
        curl_free(escaped);
    }
    curl_easy_cleanup(curl);
    sds body = makeHTTPGETCall(fullurl,resptr);
    sdsfree(fullurl);
    return body;
}
