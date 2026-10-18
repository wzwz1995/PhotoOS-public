-- 004: 任务认领需要"执行者身份"，防止多个进程重复执行同一个任务。
--
-- 触发本迁移的真实现场：`photoos serve` 的后台执行器与命令行里的
-- `photoos index`/`_full_index.py` **同时**从同一张 job 表认领任务，
-- 于是同一个任务被两个进程并行执行 —— 在只有 ~60 MB/s 的 USB 盘上等于互相抢带宽、
-- 重复读取同一批文件（实测：服务进程在跑 metadata，脚本进程在跑 fused）。
--
-- 修正：认领时记录执行者 id；只认领"无归属"或"归属自己"的任务。
-- 其他进程留下的归属会在心跳过期后被 `recover_orphans` 释放，因此崩溃不会永久占坑。

ALTER TABLE job ADD COLUMN claimed_by TEXT;

CREATE INDEX idx_job_claimed ON job(status, claimed_by);
