# A 股日线形态筛选器

基于 Python 与 AkShare 的长期运行筛选器，无需 API Token。它仅使用已完整收盘的交易日：通过交易日历选择运行当天之前最近三个开市日，若日历或任一日线快照不完整即停止，不会写入不完整结果。

## 形态规则

以连续三个交易日 `D0`、`D1`、`D2` 判断：

- `D1 close > D1 open`
- `D1 volume >= 1.5 × D0 volume`
- `D1 high > D1 close`
- `D2 high = D1 close`，且 `D2 low = D1 open`

价格相等按 A 股 `0.01` 元最小报价单位的半个 tick 容差比较，以避免浮点表示误差。筛选范围排除名称包含 `ST`（含 `*ST`）的股票、科创板及北交所；主板和创业板保留。

## 本地运行

需要 Python 3.10+：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m a_share_screener.runner
```

结果写入 `results/YYYYMMDD.csv`，日期为形态的 `D2` 交易日。数据通过 AkShare 的交易日历、A 股现货列表与不复权历史日线接口获取，无需环境变量或密钥。

运行不访问网络的单元测试：

```bash
python -m unittest discover -s tests -v
```

## GitHub Actions

工作流位于 [`.github/workflows/screen.yml`](.github/workflows/screen.yml)，会在每个工作日北京时间 17:20 运行，也可在 **Actions** 页面手动触发。无需配置 GitHub Secret。它会先由脚本核验实际交易日，随后只提交 `results/` 下有变化的 CSV。

工作流声明了最小所需的 `contents: write` 权限，仅用于把新的结果 CSV 推送回仓库。若组织策略禁用了 Actions 写权限，请在仓库 **Settings → Actions → General** 中允许工作流读写权限。
