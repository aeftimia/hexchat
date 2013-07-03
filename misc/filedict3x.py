"""filedict.py
a Persistent Dictionary in Python

Author: Erez Shinan
Date  : 31-May-2009
"""

from collections import UserDict
import pickle
import sqlite3
import hashlib

class Solutions:
    Sqlite3 = 0

class FileDict(UserDict):
    """A dictionary that stores its data persistantly in a file

    Options:
        filename - which file to use
        connection - use an existing connection instead of a filename (overrides filename)
        table - which table name to use for storing data (default: 'dict')
    
    """

    def __init__(self, filename=None, solution=Solutions.Sqlite3, **options):
        assert solution == Solutions.Sqlite3, "Only sqlite3 is supported right now"
        try:
            self.__conn = options.pop('connection')
        except KeyError:
            if not filename:
                raise ValueError("Must provide 'connection' or 'filename'")
            self.__conn = sqlite3.connect(filename)

        self.__tablename = options.pop('table', 'dict')
        
        self._nocommit = False

        assert not options, "Unrecognized options: %s" % options

        self.__conn.execute('CREATE TABLE IF NOT EXISTS %s (id integer primary key, hash integer, key blob, value blob);'%self.__tablename)
        self.__conn.execute('CREATE INDEX IF NOT EXISTS %s_index ON %s(hash);' % (self.__tablename, self.__tablename))
        self.__conn.commit()

    def _commit(self):
        if self._nocommit:
            return

        self.__conn.commit()

    def __pack(self, value):
        return pickle.dumps(value, 3)
    def __unpack(self, value):
        return pickle.loads(value)

    def __hash(self, data):
        binary_data = self.__pack(data)
        hash = int(hashlib.md5(binary_data).hexdigest(),16)
        # We need a 32bit hash:
        return hash % 0x7FFFFFFF

    def __get_id(self, key):
        cursor = self.__conn.execute('SELECT key,id FROM %s WHERE hash=?;'%self.__tablename, (self.__hash(key),))
        for k,id in cursor:
            if self.__unpack(k) == key:
                return id

        raise KeyError(key)

    def __getitem__(self, key):
        cursor = self.__conn.execute('SELECT key,value FROM %s WHERE hash=?;'%self.__tablename, (self.__hash(key),))
        for k,v in cursor:
            if self.__unpack(k) == key:
                return self.__unpack(v)

        raise KeyError(key)

    def __setitem(self, key, value):
        value_pickle = self.__pack(value)
        
        try:
            id = self.__get_id(key)
            cursor = self.__conn.execute('UPDATE %s SET value=? WHERE id=?;'%self.__tablename, (value_pickle, id) )
        except KeyError:
            key_pickle = self.__pack(key)
            cursor = self.__conn.execute('INSERT INTO %s (hash, key, value) values (?, ?, ?);'
                    %self.__tablename, (self.__hash(key), key_pickle, value_pickle) )

        assert cursor.rowcount == 1

    def __setitem__(self, key, value):
        self.__setitem(key, value)
        self._commit()

    def __delitem__(self, key):
        id = self.__get_id(key)
        cursor = self.__conn.execute('DELETE FROM %s WHERE id=?;'%self.__tablename, (id,))
        if cursor.rowcount <= 0:
            raise KeyError(key)

        self._commit()


    def update(self, d):
        for k,v in d.items():
            self.__setitem(k, v)
        self._commit()

    def __iter__(self):
        return (self.__unpack(x[0]) for x in self.__conn.execute('SELECT key FROM %s;'%self.__tablename) )
    def keys(self):
        return iter(self)
    def values(self):
        return (self.__unpack(x[0]) for x in self.__conn.execute('SELECT value FROM %s;'%self.__tablename) )
    def items(self):
        return (list(map(self.__unpack, x)) for x in self.__conn.execute('SELECT key,value FROM %s;'%self.__tablename) )

    def __contains__(self, key):
        try:
            self.__get_id(key)
            return True
        except KeyError:
            return False

    def __len__(self):
        return self.__conn.execute('SELECT COUNT(*) FROM %s;' % self.__tablename).fetchone()[0]

    def __del__(self):
        try:
            self.__conn
        except AttributeError:
            pass
        else:
            self.__conn.commit()

    @property
    def batch(self):
        return self._Batch(self)

    class _Batch:
        def __init__(self, d):
            self.__d = d

        def __enter__(self):
            self.__old_nocommit = self.__d._nocommit
            self.__d._nocommit = True
            return self.__d

        def __exit__(self, type, value, traceback):
            self.__d._nocommit = self.__old_nocommit
            self.__d._commit()
            return True
