#!/usr/bin/python3
# -*- coding: utf-8 -*-

#   Copyright 2012 codestation
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import re
import sys
import time
import signal
import shutil
import logging

from os import listdir, makedirs
from os.path import join, isdir, isfile, splitext, expanduser

from danbooru.error import DanbooruError
from danbooru.api import Api
from danbooru.database import Database
from danbooru.settings import Settings
from danbooru.downloader import Downloader
from danbooru.utils import parse_query
from danbooru.gelbooru_api import GelbooruAPI


class Daemon(object):

    _stop = False
    abort_list = {}

    config_required = [
        'api_mode',
        'host',
        'username',
        'password',
        'salt',
        ('limit', int),
        ('limit_page', int),
        'download_path',
        'log_level',
        'log_file',
        'fetch_mode',
        ('skip_file_check', bool),
    ]

    config_optional = {
                       'default_tags': None,
                       'blacklist': None,
                       'whitelist': None,
                       'dbname': None,
                        ('max_tags', int): 2,
                    }

    def parseArgs(self):

        class DummyArgs(object):
            def __init__(self):
                self.config = None
                self.section = 'danbooru'
                self.tags = None
                self.blacklist = None
                self.whitelist = None
                self.action = None
                self.before_id = None

        return DummyArgs()

    def readConfig(self, config, section, required_fields, optional_fields):
        cfg = Settings(config)
        if not cfg.load(section, required_fields, optional_fields):
            sys.exit(1)

        if "log_level" in required_fields:
            numeric_level = getattr(logging, cfg.log_level.upper(), None)
            if not isinstance(numeric_level, int):
                logging.error('Invalid log_level in config: %s' % cfg.log_level)
                sys.exit(1)
            cfg.log_level = numeric_level
        return cfg

    def parseTags(self, args, cfg):
        # use default tags from file
        if cfg.default_tags:
            default_tags = [x.strip() for x in re.sub(' +', ' ', cfg.default_tags).split(' ')]
            if not args.tags:
                args.tags = []
            args.tags = args.tags + list(set(default_tags) - set(args.tags))

        if cfg.blacklist:
            blacklist_tags = [x.strip() for x in re.sub(' +', ' ', cfg.blacklist).split(' ')]
            if not args.blacklist:
                args.blacklist = []
            args.blacklist = args.blacklist + list(set(blacklist_tags) - set(args.blacklist))

        if cfg.whitelist:
            whitelist_tags = [x.strip() for x in re.sub(' +', ' ', cfg.whitelist).split(' ')]
            if not args.whitelist:
                args.whitelist = []
            args.whitelist = args.whitelist + list(set(whitelist_tags) - set(args.whitelist))

        query = parse_query(args.tags)
        if isinstance(query, str):
            logging.error("Error in config file, malformed query: %s", query)
            sys.exit(1)

        # cut down the tag list if it have too much items
        max_tags_number = cfg.max_tags
        if query['tags'] and len(query['tags']) > max_tags_number:
            logging.warning('Using more than %i tags, cutting down list', max_tags_number)
            query['tags'] = query['tags'][:max_tags_number]

        return query

    def abort(self):
        self._stop = True
        for k in self.abort_list.keys():
            self.abort_list[k].stop()

    def registerClassSignal(self, cls):
        self.abort_list[cls.__class__.__name__] = cls

    def unregisterClassSignal(self, cls):
        del self.abort_list[cls.__class__.__name__]

    def signalHandler(self, signal, frame):  # @UnusedVariable
        print('Ctrl+C detected, shutting down...')
        self.abort()

    def getLastId(self, tag, query, board, before_id=None):
        if before_id:
            return int(before_id)
        else:
            try:
                posts = board.getPostsPage(tag, query, 1, 1)
                if posts:
                    return posts[0]['post_id'] + 1
                else:
                    logging.error('Error: cannot get last post id')
            except DanbooruError as e:
                logging.error(e.message)
            sys.exit(1)

    def main(self, path_directory: str):
        user_dir = expanduser(path_directory)
        self.args = args = self.parseArgs()

        if not args.config:
            import os
            args.config = os.path.join(os.path.dirname(__file__), '.danbooru-daemon.cfg')

        self.cfg = cfg = self.readConfig(args.config, args.section, self.config_required, self.config_optional)

        logging.basicConfig(level=cfg.log_level, filename=cfg.log_file,
                            format='%(asctime)s %(levelname)s: %(message)s',
                            datefmt='%I:%M:%S %p')

        self.query = self.parseTags(args, cfg)

        # signal.signal(signal.SIGINT, self.signalHandler)

        if not cfg.dbname:
            daemon_dir = join(user_dir, ".local/share/danbooru-daemon")
            makedirs(daemon_dir, exist_ok=True)
            cfg.dbname = join(daemon_dir, "danbooru-db.sqlite")

        self.db = db = Database(join(user_dir, cfg.dbname))
        db.setHost(cfg.host, args.section)

        if args.action == "daemon":
            self.run_daemon(args, db)
        elif args.action == "update":
            if cfg.api_mode == "danbooru":
                board = Api(cfg.host, cfg.username, cfg.password, cfg.salt)
            elif cfg.api_mode == "gelbooru":
                board = GelbooruAPI(cfg.host)
            for tag in self.query['tags']:
                print("processing tag [%s]" % tag)
                self.run_update(args, tag, cfg, board, db)
        elif args.action == "download":
            self.run_download(cfg, db)
        elif args.action == "nepomuk":
            self.run_nepomuk(cfg, db)
        elif args.action == "tags":
            board = Api(cfg.host, cfg.username, cfg.password, cfg.salt)
            self.run_tags(args, db, board)
        elif args.action == "pools":
            board = Api(cfg.host, cfg.username, cfg.password, cfg.salt)
            self.run_pools(db, board)
        elif args.action == "pool_posts":
            board = Api(cfg.host, cfg.username, cfg.password, cfg.salt)
            self.run_pool_posts(db, board)
        elif args.action == "cleanup":
            self.cleanup(cfg, db, args, cfg.download_path)

    def run_daemon(self, args, db):
        cfg = self.readConfig(args.config, "default", ['fetch_from', ('fetch_interval', int)], [])

        if not cfg.fetch_from:
            logging.error('The fetch_from config option cannot be empty in daemon mode')
            sys.exit(1)

        if not cfg.fetch_interval:
            logging.error('The fetch_interval config option cannot be empty in daemon mode')
            sys.exit(1)

        sleep_time = cfg.fetch_interval
        sections = [x.strip() for x in cfg.fetch_from.split(' ')]

        while not self._stop:
            for section in sections:
                if self._stop:
                    return
                cfg = self.readConfig(args.config, section, self.config_required, self.config_optional)
                db.setHost(cfg.host, section)
                board = Api(cfg.host, cfg.username, cfg.password, cfg.salt)
                print(">>> Run upload mode for %s", section)
                for tag in self.query['tags']:
                    print("processing tag [%s]", tag)
                    self.run_update(args, tag, cfg, board, db)
                    if self._stop:
                        return
                print(">>> Run download mode for %s", section)
                self.run_download(cfg, db)
                if self._stop:
                    return
                #print("Run nepomuk mode for %s" % section)
                #self.run_nepomuk(cfg, db)
                #if self._abort: break
            print("Waiting for %i seconds", sleep_time)
            time.sleep(sleep_time)

    def run_update(self, args, tag, cfg, board, db):
        print("run_update tag : " + tag)

        if not args.tags:
            logging.error('No tags specified. Aborting.')
            sys.exit(1)

        if not board:
            if cfg.api_mode == "danbooru":
                board = Api(cfg.host, cfg.username, cfg.password, cfg.salt)
            elif cfg.api_mode == "gelbooru":
                board = GelbooruAPI(cfg.host)

        if cfg.fetch_mode == "id":
            last_id = self.getLastId(tag, self.query, board, args.before_id)
            print('Fetching posts below id: %i', last_id)
        elif cfg.fetch_mode == "page":
            page = 1
            print('Fetching posts from page: %i', page)
        else:
            logging.error("Invalid fetch_mode")
            sys.exit(1)

        while not self._stop:
            retries = 0
            while retries < 3:
                try:
                    if cfg.fetch_mode == "id":
                        post_list = board.getPostsBefore(last_id, tag, self.query, cfg.limit, args.blacklist, args.whitelist)
                    elif cfg.fetch_mode == "page":
                        post_list = board.getPostsPage(tag, self.query, page, cfg.limit, args.blacklist, args.whitelist)
                    break
                except DanbooruError as e:
                    logging.error('>>> %s' % e.message)
                retries += 1
                logging.warning('Retrying (%i) in 2 seconds...', retries)
                time.sleep(2)
            else:
                post_list = None

            if post_list:
                start = time.time()
                results = db.savePosts(post_list)
                end = time.time() - start
                print("New entries: %i posts, %i images, %i tags", results['posts'], results['images'], results['tags'])
                print("Time taken: %.2f seconds" % end)
                # if not results['posts']:
                #     print('Stopping since no new posts were inserted')
                #     break
                if cfg.fetch_mode == "id":
                    last_id = post_list[-1]['post_id']
                    print('Fetching posts below id: %i', last_id)
                elif cfg.fetch_mode == "page":
                    page += 1
                    if cfg.limit_page < page:
                        print('Stopping since reach limit page')
                        break
                    print('Fetching posts from page: %i', page)
            else:
                print('No posts returned')
                break

    def run_download(self, cfg, db):
        dl = Downloader(expanduser(cfg.download_path))
        self.registerClassSignal(dl)
        offset = 0
        limit = 2048

        def callback(file, current, total):
            sys.stdout.write("\r%s: %i of %i bytes" % (file, current, total))
            # sys.stdout.flush()

        while not self._stop:
            rows = db.getFiles(limit, offset)
            if not rows:
                break
            dl.downloadQueue(rows, cfg.skip_file_check, callback)
            offset += limit
        self.unregisterClassSignal(dl)

    def run_nepomuk(self, cfg, db):
        from .danbooru.nepomuk import NepomukTask
        nk = NepomukTask()
        self.registerClassSignal(nk)
        nk.updateDirectoryTags(cfg.download_path, db)
        self.unregisterClassSignal(nk)

    def run_tags(self, args, cfg, db, board):  # @UnusedVariable
        last_id = self.getLastId(args.tags, board, args.before_id)
        while not self._stop:
            tagList = board.getTagsBefore(last_id, args.tags, cfg.limit)
            if tagList:
                #FIXME: implement addTags
                #db.addTags(tagList)
                last_id = tagList[-1]['id']
                print('Next fetch id: %i' % last_id)
            else:
                break

    def run_pools(self, db, board):
        page = 1
        while not self._stop:
            print('Fetching pools from page: %i', page)
            poolList = board.getPoolsPage(page)
            if poolList:
                created, updated, up_to_date = db.savePools(poolList)
                if up_to_date:
                    print('Pool list up-to-date, %i new, %i updated', created, updated)
                    break
                page += 1
            else:
                break

    def run_pool_posts(self, db, board):
        offset = 0
        limit = 1000
        pools = list()
        while not self._stop:
            pools_data = db.getPools(limit, offset)
            if not pools_data:
                break
            pools += [x.pool_id for x in pools_data]
            offset += limit
            print('Building pool list: %i...', offset)
        print('Fetching posts from %i pools', len(pools))
        for pool in pools:
            page = 1
            count = 0
            total = 0
            while not self._stop:
                print('Fetching from pool: %i, posts from page: %i', pool, page)
                posts = board.getPoolPostsPage(pool, page)
                if posts:
                    if page == 1:
                        db.savePool(pool, posts_id=None, modified=True)
                    count += db.savePool(pool, posts, modified=True)
                    page += 1
                    total += len(posts)
                else:
                    count += db.savePool(pool)
                    print('Got %i/%i posts from pool %i', count, total, pool)
                    break

    def clean_loop(self, directory, dest, db):
        count = 0
        for name in listdir(directory):
            if self._stop:
                break
            full_path = join(directory, name)
            if isdir(full_path):
                count += self.clean_loop(full_path, dest, db)
            elif isfile(full_path):
                md5 = splitext(name)[0]
                if not db.fileExists(md5):
                    print('%s isn\'t in database', name)
                    shutil.move(full_path, join(dest, name))
                    count += 1
        return count

    def cleanup(self, cfg, db, args, dest):
        post_c, img_c, tag_c = db.deletePostsByTags(args.blacklist, args.whitelist)
        print('Deleted %i posts, %i images refs, %i tags', post_c, img_c, tag_c)

        count = self.clean_loop(cfg.download_path, dest, db)
        print('Moved %i images', count)

if __name__ == '__main__':
    Daemon().main()
