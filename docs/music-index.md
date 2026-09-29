# 可选选曲索引

普通 `propcut process`、`stitch` 和显式指定曲目的 `audition --tracks ...` 不需要这个数据库。
`suggest` 和没有 `--tracks` 的 audition 才需要兼容的只读索引。软件不附带私人索引，也不会自动扫描或导入用户音乐。

目录约定：music.library 指向 `<root>/music/01_approved/commercial`，分类子目录包含用户有权使用的音频；索引放在 `<root>/db/music_library.sqlite`。

最小结构如下，可在自己建立的空索引中使用。不要对已有业务数据库执行这些语句。

```sql
CREATE TABLE tracks (
  id INTEGER PRIMARY KEY, title TEXT, artist TEXT,
  local_file_path TEXT, duration_seconds REAL, bpm REAL,
  status TEXT, usage_tier TEXT, category TEXT
);
CREATE TABLE track_analysis (
  id INTEGER PRIMARY KEY, track_id INTEGER,
  bpm REAL, rms_energy REAL, vocals_p REAL, red_flags TEXT
);
```

local_file_path 是用户音频的完整路径；category 对应分类目录。分类推荐只取 status=approved、usage_tier=commercial，且具备对应分析记录的曲目；vocals_p 不得为空且需通过阈值，red_flags 为 JSON 数组，非空表示排除。文件必须真实存在且位于配置的曲库中。精选池还要求用户提供 pools_file，内置池为空。

这些分类与分析值必须由用户自己的可信流程提供。写 commercial 或 approved 不会产生版权授权，工具不会代为判定音乐可商用。索引缺失时推荐返回不可用；普通处理可继续使用显式音乐文件或关闭音乐。推荐器使用只读连接，不维护索引。

最小 schema 的单元验证使用临时虚拟曲库和合成记录，见 tests/test_propcut_suggest.py。未附带音频分析器、索引管理界面或真实曲库数据。
