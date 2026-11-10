CREATE TABLE IF NOT EXISTS map_meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL) WITHOUT ROWID;
INSERT OR IGNORE INTO map_meta VALUES('next_generation',1);
CREATE TABLE IF NOT EXISTS generations(id INTEGER PRIMARY KEY,volume INTEGER,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS volumes(
 key INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,label TEXT NOT NULL,mount TEXT NOT NULL,generation INTEGER,
 observed_ns INTEGER,full_ns INTEGER,volume_used INTEGER,capacity INTEGER,free INTEGER,available INTEGER,
 valid INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,lean INTEGER NOT NULL,bookmark INTEGER,
 no_progress INTEGER NOT NULL DEFAULT 0,event_uuid TEXT,gaps TEXT NOT NULL DEFAULT '[]',mounted INTEGER NOT NULL DEFAULT 1,full_attempt_ns INTEGER,budget_retry_bytes INTEGER,delta_generation INTEGER);
CREATE TABLE IF NOT EXISTS nodes(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,id INTEGER NOT NULL,parent INTEGER,label TEXT,kind TEXT NOT NULL,
 own_listed INTEGER NOT NULL,own_uses INTEGER NOT NULL,listed INTEGER,uses INTEGER,status TEXT NOT NULL,mtime INTEGER,
 PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS node_parent ON nodes(generation,volume,parent,label);
CREATE INDEX IF NOT EXISTS node_size ON nodes(generation,volume,parent,uses DESC,id DESC);
CREATE INDEX IF NOT EXISTS node_listed ON nodes(generation,volume,parent,listed DESC,id DESC);
-- Coverage counts excluded roots without scanning the whole map.
CREATE INDEX IF NOT EXISTS node_excluded ON nodes(generation,volume) WHERE status='excluded';
CREATE TABLE IF NOT EXISTS files(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,parent INTEGER NOT NULL,id INTEGER NOT NULL,label TEXT,
 allocated INTEGER NOT NULL,mtime INTEGER,PRIMARY KEY(generation,volume,parent,id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS file_size ON files(generation,volume,parent,allocated DESC,id DESC);
CREATE TABLE IF NOT EXISTS holders(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,dir INTEGER NOT NULL,file_id INTEGER NOT NULL,
 stream INTEGER NOT NULL,allocated INTEGER NOT NULL,private INTEGER,partial INTEGER NOT NULL,
 PRIMARY KEY(generation,volume,dir,file_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS holder_stream ON holders(generation,volume,stream);
CREATE INDEX IF NOT EXISTS holder_file ON holders(generation,volume,file_id,dir);
CREATE TABLE IF NOT EXISTS charges(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,stream INTEGER NOT NULL,charge INTEGER NOT NULL,
 allocated INTEGER NOT NULL,private INTEGER,partial INTEGER NOT NULL,dir INTEGER NOT NULL,
 PRIMARY KEY(generation,volume,stream)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS listings(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,id INTEGER NOT NULL,listed_ns INTEGER,count INTEGER,
 PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS removed(generation INTEGER,volume INTEGER,id INTEGER,PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS reset_dirs(generation INTEGER,volume INTEGER,id INTEGER,PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS removed_charges(generation INTEGER,volume INTEGER,id INTEGER,PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tracked(volume TEXT,id TEXT,parent TEXT,PRIMARY KEY(volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS points(
 volume TEXT NOT NULL,id TEXT NOT NULL,observed_ns INTEGER NOT NULL,label TEXT,metric TEXT NOT NULL,
 size INTEGER,status TEXT NOT NULL,PRIMARY KEY(volume,id,observed_ns)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hierarchy_epochs(
 volume TEXT NOT NULL,observed_ns INTEGER NOT NULL,version TEXT NOT NULL,PRIMARY KEY(volume,observed_ns)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hierarchy_nodes(
 volume TEXT NOT NULL,version TEXT NOT NULL,id TEXT NOT NULL,parent TEXT,label TEXT,present INTEGER NOT NULL,
 rank INTEGER NOT NULL,first_rank INTEGER NOT NULL,PRIMARY KEY(volume,version,rank),UNIQUE(volume,version,id)) WITHOUT ROWID;
-- recursive NULL is a deferral recorded before the flag existed: treated as recursive.
CREATE TABLE IF NOT EXISTS dirty(volume TEXT,id TEXT,due_ns INTEGER,recursive INTEGER,PRIMARY KEY(volume,id)) WITHOUT ROWID;
-- The last time a volume's root was refused, kept apart from its published map's times.
CREATE TABLE IF NOT EXISTS denied(volume TEXT PRIMARY KEY,observed_ns INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY,started_ns INTEGER NOT NULL,updated_ns INTEGER NOT NULL,ended_ns INTEGER,
 pid INTEGER NOT NULL,pid_start TEXT,status TEXT NOT NULL,entries INTEGER NOT NULL DEFAULT 0,cpu_ns INTEGER NOT NULL,
 peak_rss_bytes INTEGER NOT NULL,written_bytes INTEGER,kind TEXT NOT NULL,
 volume_id TEXT,action TEXT,trigger TEXT,stage TEXT,volumes TEXT) WITHOUT ROWID;
