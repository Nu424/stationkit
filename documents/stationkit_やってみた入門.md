# 「接続して、選んで、動かす」を一発で形にしたい日の stationkit 入門（やってみた）

> この記事は **架空の体験談テイスト** で書いています。実際のプロジェクト名・装置名はフィクションですが、API の挙動は [README.md](../README.md) とコードに沿った内容です。

---

## はじめに：なぜ stationkit を知ったか

仮想の話ですが、研究室の **後輩くん** がこう言いました。

「オートサンプラーみたいなやつ、**ポートを切り替えてから本番の動きを一回走らせる**、みたいな流れが毎回同じなんですよ。Python でバッチも書くし、あとから **Web で触れるようにもしたい** んですけど……同期と非同期と HTTP と GUI で、同じロジックを何回書くんだ問題が発生してます」

そこで出てきたのが **stationkit** です。一言でいうと、

- **接続（connect）→ 対象選択（change）→ 実行（execute）** という型を装置側に押し込んで、
- その実装 **1 本**から、ライブラリ直叩き / HTTP / CLI / Gradio GUI をまとめて生やせる

という小さめのフレームワークです。🧪

この記事では、後輩くんが触ることになる **架空の分流装置「AS-200」**（3 ポート切替＋サンプリング実行のイメージ）を例に、**「まず動くモック」→「自分のコントローラ」→「HTTP / CLI / GUI」→「長い実行とキャンセル」→「便利な独自ボタン」** の順で進めます。

---

## 30 秒でわかる：最小のサンプル（何ができるか）

「全部読む前に、コードの匂いだけ掴みたい」向けです。**実機なし**で、ライブラリとしての一番短い流れは次のとおりです。

```python
from stationkit import MockStationController

# 接続 → どのステーションか指定 → 本番の一発 → 状態確認 → 切断
c = MockStationController()
c.connect("DEMO")
c.change(2)
print(c.execute())
print(c.status())
c.disconnect()
```

もう一歩だけ進めると、**同じ操作を Web API にしたい**ときは、コントローラを 1 個用意して FastAPI アプリに包む、という形になります（後半の第 4 章で詳しく）。

```python
from stationkit import create_http_app, MockStationController

controller = MockStationController()
app = create_http_app(controller)
# あとは uvicorn で app を起動すれば POST /connect などが使える
```

ここまでが「ざっくり全体像」です。以降の章では、後輩くんの **AS-200 用の本物に近いコントローラ**を書きながら、同じ流れを深掘りします。

---

## 特徴をざっくり掴む（ここがラク）

| 嬉しい点 | ざっくり説明 |
|----------|----------------|
| **1 実装・4 経路** | `StationControllerBase` を書けば、同じ操作がライブラリ / HTTP / CLI / GUI に出る |
| **async を 1 回だけ** | `_do_*` は **async で 1 セット**。同期 API（`connect` など）と `*_async` は基底が用意 |
| **型ヒントがそのまま UI** | `_do_change` の `target` 型や、execute の Pydantic モデルが CLI/HTTP/GUI の入力に反映 |
| **長時間実行は別スレッド** | `execute()` は「待つ」まま。HTTP/GUI 側は `ExecutionManager` で非ブロッキング開始・状態取得・協調 cancel |
| **CustomAction で拡張** | 装置固有コマンドをスキーマ付きで一括公開 |

> **💡 ヒント**  
> 「全部を最初から使う」必要はありません。**まず `MockStationController` で流れを覚えて**、装置クラスを差し替える、が一番ストレスが少ないです。

---

## 第 1 章：まずは実機なしで「型」を体に染み込ませる（後輩くんの予習）

依存は [uv](https://github.com/astral-sh/uv) 前提で、`uv sync` で OK です（Python 3.12 以上）。

```python
from stationkit import MockStationController

c = MockStationController()
c.connect("DEMO")
c.change(2)
print(c.execute())
print(c.status())
c.disconnect()
```

ここで体感するのは **状態の流れ** です。後輩くんが AS-200 を触るときも同じ順番になります。

1. `connect` しないと `change` / `execute` は進めない（基底が状態を見る）
2. `change` で「どのポート／ステーションか」をセット
3. `execute` が「本番の一発」（サンプリング開始など）

> **⚠️ 注意（同期 vs 非同期）**  
> **すでに asyncio のイベントループが動いている**場所（FastAPI のハンドラ内など）では、同期の `connect()` は **`RuntimeError`** になります。そのときは `await connect_async()` を使ってください。

---

## 第 2 章：後輩くんの AS-200 用コントローラを書いてみる

ここからは **自分の `StationControllerBase`** です。ルールはシンプルで、**`_do_connect` / `_do_disconnect` / `_do_change` / `_do_execute` / `_do_status` の 5 つをすべて async で実装**します。

```python
from typing import Any

from stationkit import StationControllerBase


class AS200Controller(StationControllerBase):
    """架空：研究室の分流装置。3 ポートに切り替えてから擬似計測を 1 回だけ走らせる。"""

    def __init__(self) -> None:
        super().__init__()
        self._address: str | None = None
        self._port: int | None = None

    async def _do_connect(self, address: str) -> None:
        self._address = address

    async def _do_disconnect(self) -> None:
        self._address = None
        self._port = None

    async def _do_change(self, target: int) -> None:
        # target の型ヒント (int) が、そのまま HTTP/CLI/GUI の「切替引数」の型になる
        if target not in (1, 2, 3):
            raise ValueError("port must be 1..3")
        self._port = target

    async def _do_execute(self) -> dict[str, Any]:
        return {"address": self._address, "port": self._port, "done": True}

    async def _do_status(self) -> dict[str, Any]:
        return {"port": self._port}
```

### ここが地味に効く：`_do_change` の型ヒント

`_do_change(self, target: int)` の **`int` がそのまま公開 API の型**になります。型を **`Any` のまま**にしておくと、警告のうえ **`str` 扱い** に寄せられてしまいます。

> **🙈 よくある間違い**  
> 「とりあえず `Any` で書いて後で直す」を放置すると、CLI や HTTP の入力が **意図せず文字列中心**になって、後から直すコストが出やすいです。**最初から具体型**（`int` や `str`、必要なら Enum）を付けるのがおすすめ。

---

## 第 3 章：execute にパラメータを足す（攪拌の秒数など）

後輩くんの実験では、「**攪拌 5 秒、回転数 120**」みたいな値を毎回変えたい、となりました。`_do_execute` に **Pydantic モデル 1 個**を足します。

```python
from pydantic import BaseModel


class RunParams(BaseModel):
    duration_s: int
    rpm: int | None = None


class AS200Controller(StationControllerBase):
    # _do_connect などは第 2 章と同じ想定で省略

    async def _do_execute(self, params: RunParams) -> dict[str, Any]:
        return {
            "address": self._address,
            "port": self._port,
            "duration_s": params.duration_s,
            "rpm": params.rpm,
        }
```

呼び出し側は次のように揃います。

- **Python 直叩き**: `execute(RunParams(...))` または `execute({"duration_s": 5, "rpm": 120})`
- **HTTP**: `POST /execute` に JSON ボディ
- **CLI**: `execute` サブコマンドに **JSON 文字列 1 個**（例: `'{"duration_s": 5, "rpm": 120}'`）

> **💡 ヒント**  
> `_do_execute(self, params: RunParams | None = None)` のように **Optional** にすると、引数なしの `execute()` も運用できます。

---

## 第 4 章：HTTP で立ち上げて、研究室の別マシンからも触る

後輩くんは、自分の PC から `requests` で叩き、先輩は別 PC のブラウザから同じ API を叩く、といった使い分けをしたい、という話になりました。

```python
from stationkit import create_http_app

from mylab.as200 import AS200Controller

controller = AS200Controller()
app = create_http_app(controller)
```

`uvicorn` で起動すれば、`POST /connect` → `POST /change` → `POST /execute` の流れが JSON で叩けます。エンドポイントの一覧は [README.md の表](../README.md#主なエンドポイント)にまとまっています 🗺️

> **⚠️ 注意**  
> JSON の `POST` は **`Content-Type: application/json`** を付けるのが前提です。

装置側の例外は、可能なら **`ConnectionError` / `CommandError` / `TimeoutError` / `StateError` など stationkit が用意している型**に寄せると、HTTP のステータスコードとの対応が分かりやすくなります。

---

## 第 5 章：CLI を使うときに押さえる「1 つの誤解」

ここは README の一行だけだと「何のこと？」となりやすいので、**動きのイメージ**から説明します。

### 誤解しやすい点：ターミナルでコマンドを打つたびに「記憶がリセット」される

普通の CLI ツールは、`python 何か.py connect COM3` を実行するたびに **新しい Python プロセス**が立ち上がります。プロセスが終わるとメモリ上の変数は消えるので、**さっき `connect` したはずなのに、次の `change` のときには接続情報が無い**、ということが起きます。

後輩くんがやりたいのは、「一度 `connect` したあと、何度も `change` や `execute` を叩きたい」なので、**どこかに「今つないでいる COM ポート」を覚えておく場所**が要ります。

### stationkit の CLI が用意している形

`create_cli_app()` で作る CLI には、ざっくり次の二つの顔があります。

1. **`serve`（いわゆる窓口モード）**  
   ずっと動き続ける **小さなサーバープロセス**が 1 つ立ち上がり、その中に **AS-200 のコントローラが 1 個**ずっといます。ここが「接続状態」を覚えています。

2. **それ以外のサブコマンド（`connect` / `change` / `execute` など）**  
   毎回すぐ終わるコマンドですが、内部では **その窓口サーバーに HTTP でお願い**します。だから「前のプロセスの記憶」に頼らず、**窓口が覚えている状態**を使えます。

イメージとしては、**受付に常駐スタッフがいて、接続やポート切替のメモを預かってくれる**感じです。後輩くんはターミナルを二つ開けて、片方で `serve`、もう片方で `connect` → `change` → … と打つ、という使い方になります。

```python
from stationkit import create_cli_app

from mylab.as200 import AS200Controller

app = create_cli_app(AS200Controller())

if __name__ == "__main__":
    app()
```

サーバーの URL は `--server` か、環境変数 `STATIONKIT_SERVER_URL` でも渡せます。

### じゃあ「窓口」なしでいいのはどんなとき？

**1 本の Python スクリプトの中で** `connect` → `change` → `execute` → `disconnect` まで全部書くなら、そもそも **ライブラリを普通に import して呼ぶ**だけで足ります（第 1 章の `MockStationController` と同じノリ）。その場合はプロセスが 1 つなので、変数に状態が残ります。

逆に、「CI から `execute` だけ叩きたいが、その前の `connect` は別ジョブで済ませたい」みたいに **プロセスをまたぐ**なら、窓口モードのような仕組みが必要、という整理になります。

> **🙈 よくある間違い**  
> `serve` を立てずに、毎回 `python mycli.py connect` だけを単発実行して「なぜか繋がらない」となる、が起きがちです。**状態を跨いで使うなら、先に窓口（`serve`）を起動する**のが先です。

### おまけ：デバッグ用の「全部このプロセスだけで完結」CLI

**同じ Python プロセス内**でコントローラを共有したいだけなら `create_local_cli_app()` があります。自動テストや、REPL でじゃばじゃば試す用途のイメージです。

---

## 第 6 章：Gradio GUI でブラウザから触る（後輩くんのデスク画面）

```python
from stationkit import create_gui_app

from mylab.as200 import AS200Controller

controller = AS200Controller()
app = create_gui_app(controller)
app.launch()
```

GUI の入力は、**単純な型（文字列・数値など）ならフォームが自動**で、複雑な型は **JSON テキスト入力にフォールバック**します（詳細は README の「入力フォームの自動生成ルール」）。

> **⚠️ 注意**  
> ブラウザを複数開いても、見ているのは **同じコントローラ実体**です。画面の状態というより、**装置側の接続・ポート・ジョブ**が本体。研究室で二人同時に触ると「おっさきに〜」が必要になる、という意味ではオートサンプラー実機と同じです 👥

`Start Execute` は内部で `ExecutionManager` を使い、**ボタンの処理がずっと固まらない**ようにしています（次の第 7 章の話につながります）。

---

## 第 7 章：長い execute と ExecutionManager（サンプリングが長い日の話）

### 後輩くんの悩み：「待つ API」と「固まらせたくない画面」

`controller.execute()` は、**終わるまで返ってこない**（＝呼び出し元はその間待つ）という形です。測定スクリプトだけならそれで問題ありません。

一方、**ブラウザのボタン**や **HTTP のリクエスト**で `execute` を直に叩くと、処理が長いときに **接続が長時間ブロック**したり、タイムアウトしたりしがちです。

そこで stationkit は **`ExecutionManager`** という別レイヤーを用意しています。ざっくり言うと、

- **裏側の別スレッド**で `controller.execute()` 全体を走らせる
- 手元では **`execution_id` をもらって、あとから状態を聞きに行く**

という二段構えです。GUI の「Start Execute」や HTTP の `POST /execute/start` は、この考え方に乗っています。

> **⚠️ まず押さえること**  
> - `ExecutionManager` が動かしているのは **`controller.execute()` 一式**です。`_do_execute` を直接スレッドから呼び分けているわけではありません。  
> - `get_status()` が返すのは **ジョブ管理の状態**（走っているか、成功したか、など）。装置の生データはこれまで通り `controller.status()` 側です。  
> - **同時に複数ジョブを積む設計ではありません**。すでに実行中に `start()` しようとすると `StateError` になります。

### サンプル：攪拌つきの長い execute をキャンセルまで含めて回す

後輩くんの AS-200 は、第 3 章の `RunParams` で攪拌時間を指定します。ここでは **1 秒ずつ区切って待ちながら、キャンセルフラグを見る**ようにして、途中で止められるようにします。

**同期メソッド `cancel_execution()`** を実装すると、`ExecutionManager` からの中断要求に応えられます（中で装置に「止めて」と送る想定）。`_do_execute` 側は **`ExecutionCancelledError`** を上げると、ジョブがきれいに **CANCELLED** として終わります。

```python
import asyncio
from typing import Any

from pydantic import BaseModel

from stationkit import (
    ExecutionCancelledError,
    ExecutionManager,
    ExecutionState,
    StationControllerBase,
)


class RunParams(BaseModel):
    duration_s: int
    rpm: int | None = None


class AS200LongRunController(StationControllerBase):
    """第2〜3章の AS-200 を拡張：長い execute と協調キャンセル。"""

    def __init__(self) -> None:
        super().__init__()
        self._address: str | None = None
        self._port: int | None = None
        self._cancel_requested = False

    async def _do_connect(self, address: str) -> None:
        self._address = address

    async def _do_disconnect(self) -> None:
        self._address = None
        self._port = None

    async def _do_change(self, target: int) -> None:
        if target not in (1, 2, 3):
            raise ValueError("port must be 1..3")
        self._port = target

    def cancel_execution(self) -> None:
        """ExecutionManager から呼ばれる。装置へ止め指令を出す想定。"""
        self._cancel_requested = True

    async def _do_execute(self, params: RunParams) -> dict[str, Any]:
        self._cancel_requested = False
        for _ in range(params.duration_s):
            if self._cancel_requested:
                raise ExecutionCancelledError("Sampling cancelled by operator.")
            await asyncio.sleep(1)
        return {
            "address": self._address,
            "port": self._port,
            "duration_s": params.duration_s,
            "rpm": params.rpm,
            "done": True,
        }

    async def _do_status(self) -> dict[str, Any]:
        return {"port": self._port}
```

**スクリプト側**では、接続してから `ExecutionManager` を回します。`get_status()` をループで見て、終了したら結果を読む、という形です。

```python
from stationkit import ExecutionState

ctrl = AS200LongRunController()
ctrl.connect("COM3")
ctrl.change(1)

manager = ExecutionManager(ctrl)
handle = manager.start(RunParams(duration_s=60, rpm=120))

while True:
    status = manager.get_status(handle.execution_id)
    print(status.state)
    if status.state not in {ExecutionState.RUNNING, ExecutionState.CANCELLING}:
        break
    # 実運用では time.sleep などでポーリング間隔を空ける

if status.state == ExecutionState.SUCCEEDED:
    print(status.result)
elif status.state == ExecutionState.CANCELLED:
    print("きちんと CANCELLED で終了:", status.error_message)
```

途中で止めたいときは **`cancel_execution()` を実装済み**であることが前提で、`manager.cancel(handle.execution_id)` を呼びます（GUI の Cancel も同じ系統です）。

> **🙈 よくある間違い**  
> `cancel_execution()` を **実装していない**コントローラに対して `cancel()` すると、**未対応**としてエラーになります。止めたいなら、装置側の中断とセットでフックを書く必要があります。

HTTP から同じことをするなら、`POST /execute/start` で `execution_id` をもらい、`GET /execute/status` で様子を見て、`POST /execute/cancel` で止める、という流れになります（研究室の別 PC から後輩くんがポーリングする、という絵も描けます）。

---

## 第 8 章：CustomAction で「本体の流れ」とは別の便利ボタンを足す

`connect → change → execute` がメインの流れですが、現場では **「ラインをリンスするだけ」「センサの生値を 1 回だけ読む」**など、メインの execute とは別枠の操作が欲しくなることがあります。

stationkit では **`get_custom_actions()`** に **`CustomAction`** を並べると、その操作も HTTP / CLI / GUI にまとめて出ます。後輩くんの AS-200 では、例として **リンス**と **センサ疎通** を足してみます。

```python
from typing import Any

from pydantic import BaseModel

from stationkit import CustomAction, StationControllerBase


class EmptyIn(BaseModel):
    """引数なしの操作向け（フィールドなしでよい）。"""


class RinseDone(BaseModel):
    message: str


class SensorPingOut(BaseModel):
    ok: bool
    raw_mv: float


class AS200WithActions(StationControllerBase):
    """第2章の AS-200 に、リンスとセンサ疎通を足した例。"""

    def __init__(self) -> None:
        super().__init__()
        self._address: str | None = None
        self._port: int | None = None

    async def _do_connect(self, address: str) -> None:
        self._address = address

    async def _do_disconnect(self) -> None:
        self._address = None
        self._port = None

    async def _do_change(self, target: int) -> None:
        if target not in (1, 2, 3):
            raise ValueError("port must be 1..3")
        self._port = target

    async def _do_execute(self) -> dict[str, Any]:
        return {"address": self._address, "port": self._port, "done": True}

    async def _do_status(self) -> dict[str, Any]:
        return {"port": self._port}

    async def _do_rinse_line(self, _params: EmptyIn) -> RinseDone:
        # 実運用では弁を開閉して水路を流す、など
        return RinseDone(message="rinse sequence finished (stub)")

    async def _do_ping_sensor(self, _params: EmptyIn) -> SensorPingOut:
        return SensorPingOut(ok=True, raw_mv=123.4)

    def get_custom_actions(self) -> list[CustomAction]:
        return [
            CustomAction(
                name="rinse_line",
                description="配管をリンス（メインの execute とは別）",
                func=self._do_rinse_line,
                input_schema=EmptyIn,
                output_schema=RinseDone,
            ),
            CustomAction(
                name="ping_sensor",
                description="センサ疎通チェック",
                func=self._do_ping_sensor,
                input_schema=EmptyIn,
                output_schema=SensorPingOut,
            ),
        ]
```

### どう呼ぶか（後輩くんの操作イメージ）

- **HTTP**: `POST /actions/rinse_line` で、入力がフィールド無しならボディは `{}`。
- **CLI**: サブコマンド名が `rinse_line` になり、**JSON 文字列 1 引数**です。引数が空でも **`'{}'` と書く**のがポイントです。

```text
rinse_line '{}'
ping_sensor '{}'
```

> **🙈 よくある間違い**  
> CLI では `rinse_line` だけ Enter、だと足りず、**空でも `'{}'` を付ける**必要があります。最初はここで「動かない？」となりやすいです。

---

## おわりに：この記事のゴールは「迷子ポイントの地図」

stationkit は、装置制御の **型（接続→選択→実行）** を先に決めて、その上に **アダプタを載せる**のが得意です。だから逆に言うと、

- **そもそも操作がこの型に収まらない**装置だと窮屈に感じるかもしれません
- **型ヒントや Pydantic をきちんと書くほど**、HTTP/CLI/GUI が楽になる
- **async 内では同期 API を使わない**、は最初の一回でつまずきやすい

といった「落とし穴」もセットでついてきます。

> **💡 最後のヒント**  
> テストは `MockStationController` と `pytest` で回せます。実機がなくても、**状態遷移とアダプタの振る舞い**はかなり検証しやすいです。

もし「うちの装置はこういう理由で型に収まりにくい」みたいな話があれば、設計の背景は [station_controller_design.md](./station_controller_design.md) が深掘り向きです。📚

---

## 参考リンク

- パッケージの公式 README: [README.md](../README.md)
- 設計ドキュメント: [station_controller_design.md](./station_controller_design.md)
