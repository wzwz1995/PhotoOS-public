"""断点测试包：需要真实照片库，默认跳过。

用法：

```powershell
$env:PHOTOOS_BREAKPOINT_LIBRARY = "1"
$env:PHOTOOS_BREAKPOINT_PERIODS = "2"
python -m pytest -m breakpoint
```

完整验收（含报告生成）请用 CLI：

```powershell
photoos breakpoint --library 1 --periods 4
```
"""
