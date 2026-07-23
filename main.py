# main.py
# 라즈베리파이 피코 2 W - LED 기억력 낚시 게임 + 웹 대시보드
# WS2813 LED 바 (18번 핀, 60개) 사용

import network
import socket
import time
import random
import machine
from machine import Pin
import rp2
from rp2 import PIO, asm_pio

from wifi_config import WIFI_SSID, WIFI_PASSWORD

# ------------------------------------------------------------
# 1. WS2813 LED 제어 (PIO 사용, 타이밍 값 고정)
# ------------------------------------------------------------
LED_PIN = 18
NUM_LEDS = 60
MAX_BRIGHTNESS = 60  # 최대 밝기 (0~255 중 낮게 설정)

# WS2813 타이밍용 PIO 프로그램
# 지정된 네 개의 타이밍 값(280, 515, 515, 745)을 사용
T0H = 280
T0L = 515
T1H = 515
T1L = 745


@asm_pio(sideset_init=PIO.OUT_LOW, out_shiftdir=PIO.SHIFT_LEFT, autopull=True, pull_thresh=24)
def ws2813():
    T1 = 2
    T2 = 3
    T3 = 3
    wrap_target()
    label("bitloop")
    out(x, 1).side(0)[T3 - 1]
    jmp(not_x, "do_zero").side(1)[T1 - 1]
    jmp("bitloop").side(1)[T2 - 1]
    label("do_zero")
    nop().side(0)[T2 - 1]
    wrap()


class WS2813:
    def __init__(self, pin_num, num_leds):
        self.num_leds = num_leds
        self.sm = rp2.StateMachine(
            0, ws2813, freq=8_000_000,
            sideset_base=Pin(pin_num)
        )
        self.sm.active(1)
        self.buf = [(0, 0, 0)] * num_leds

    def set_pixel(self, i, r, g, b):
        if 0 <= i < self.num_leds:
            self.buf[i] = (r, g, b)

    def fill(self, r, g, b):
        for i in range(self.num_leds):
            self.buf[i] = (r, g, b)

    def clear(self):
        self.fill(0, 0, 0)

    def write(self):
        # GRB 순서로 전송 (WS2813 표준)
        for r, g, b in self.buf:
            grb = (g << 16) | (r << 8) | b
            self.sm.put(grb << 8)
        time.sleep_us(300)  # 리셋 신호(래치)


led = WS2813(LED_PIN, NUM_LEDS)


def scale(value, brightness=MAX_BRIGHTNESS):
    """0~255 값을 밝기 제한에 맞게 조정"""
    return int(value * brightness / 255)


def random_sample(pool_size, k):
    """MicroPython에는 random.sample()이 없으므로 직접 구현
    0 ~ pool_size-1 범위에서 서로 다른 k개의 숫자를 무작위로 뽑음"""
    pool = list(range(pool_size))
    result = []
    for _ in range(k):
        idx = random.randint(0, len(pool) - 1)
        result.append(pool.pop(idx))
    return result


def shuffle_list(lst):
    """MicroPython에는 random.shuffle()이 없을 수 있으므로 직접 구현"""
    n = len(lst)
    for i in range(n - 1, 0, -1):
        j = random.randint(0, i)
        lst[i], lst[j] = lst[j], lst[i]


def chunk_list(lst, size):
    """리스트를 size개씩 묶어서 리스트의 리스트로 반환"""
    chunks = []
    for i in range(0, len(lst), size):
        chunks.append(lst[i:i + size])
    return chunks


# ------------------------------------------------------------
# 2. 게임 상태 변수 (기억력 게임 버전 - 2개씩 순차 점등)
# ------------------------------------------------------------
# 게임 단계(phase) 정의
PHASE_WAITING = "waiting"      # 대기 중 (시작 전)
PHASE_SHOWING = "showing"      # 물고기들이 LED에 2개씩 순서대로 표시되는 중
PHASE_GUESSING = "guessing"    # 플레이어가 답을 입력하는 중
PHASE_RESULT = "result"        # 정답/오답 결과를 보여주는 중

# 순차 점등 타이밍 설정 (더 빨리 사라지도록 단축)
LIGHT_ON_MS = 300    # 물고기 묶음이 켜져 있는 시간
LIGHT_OFF_MS = 150   # 다음 묶음을 켜기 전 어둡게 쉬는 시간

GROUP_SIZE = 2       # 한 번에 동시에 표시할 물고기 개수
NUM_FAKE = 7         # 가짜 물고기 개수 (진짜 1마리 포함하여 총 8마리)


class GameState:
    def __init__(self):
        self.score = 0
        self.round_num = 0
        self.reset_round()
        self.phase = PHASE_WAITING

    def reset_round(self):
        self.phase = PHASE_WAITING
        self.real_fish_position = None   # 진짜 물고기 위치
        self.show_groups = []             # [[(위치, 진짜여부), ...], ...] 그룹 목록
        self.show_index = 0               # 현재 몇 번째 그룹을 보여주는 중인지
        self.show_lit = False              # 현재 켜진 상태인지 꺼진 상태인지
        self.show_next_time = 0           # 다음 상태 전환 시각
        self.result_until = 0
        self.last_correct = None          # 마지막 라운드 정답 여부
        self.last_answer = None           # 플레이어가 제출한 답

    def start_new_round(self, num_fake=NUM_FAKE, group_size=GROUP_SIZE):
        """새로운 라운드를 시작 - 가짜/진짜 물고기 위치를 무작위로 정하고
        2개씩 묶어서 순서대로 보여주기 시작"""
        self.round_num += 1
        positions = random_sample(NUM_LEDS, num_fake + 1)
        self.real_fish_position = positions[0]
        fake_positions = positions[1:]

        # (위치, 진짜인지 여부) 목록을 만들고 순서를 섞음
        sequence = [(self.real_fish_position, True)]
        for pos in fake_positions:
            sequence.append((pos, False))
        shuffle_list(sequence)

        # group_size개씩 묶음
        self.show_groups = chunk_list(sequence, group_size)
        self.show_index = 0
        self.show_lit = True  # 첫 번째 그룹부터 바로 켜서 보여줌
        self.show_next_time = time.ticks_add(time.ticks_ms(), LIGHT_ON_MS)
        self.phase = PHASE_SHOWING
        self.last_correct = None
        self.last_answer = None

    def submit_answer(self, answer):
        """플레이어가 제출한 답(칸 번호)을 채점"""
        if self.phase != PHASE_GUESSING:
            return
        self.last_answer = answer
        if answer == self.real_fish_position:
            self.score += 1
            self.last_correct = True
        else:
            self.last_correct = False
        self.result_until = time.ticks_add(time.ticks_ms(), 2000)
        self.phase = PHASE_RESULT


game = GameState()


# ------------------------------------------------------------
# 3. 게임 로직 업데이트 (단계 전환 처리)
# ------------------------------------------------------------
def game_step():
    now = time.ticks_ms()

    if game.phase == PHASE_SHOWING:
        if time.ticks_diff(now, game.show_next_time) >= 0:
            if game.show_lit:
                # 켜져 있던 상태 -> 끄고, 잠깐 쉬었다가 다음으로
                game.show_lit = False
                game.show_next_time = time.ticks_add(now, LIGHT_OFF_MS)
            else:
                # 꺼진 상태 -> 다음 그룹으로 넘어감
                game.show_index += 1
                if game.show_index >= len(game.show_groups):
                    # 모든 그룹을 다 보여줬으면 답 맞히기 단계로 전환
                    game.phase = PHASE_GUESSING
                else:
                    game.show_lit = True
                    game.show_next_time = time.ticks_add(now, LIGHT_ON_MS)

    elif game.phase == PHASE_RESULT:
        # 결과 표시 시간이 끝나면 -> 다음 라운드 자동 시작
        if time.ticks_diff(now, game.result_until) >= 0:
            game.start_new_round()


# ------------------------------------------------------------
# 4. LED 렌더링 (물고기 위치는 오직 여기, LED에서만 표시됨)
# ------------------------------------------------------------
def render():
    led.clear()

    if game.phase == PHASE_WAITING:
        # 대기 화면: 파란색 은은하게
        b = scale(80)
        for i in range(NUM_LEDS):
            led.set_pixel(i, 0, 0, b)

    elif game.phase == PHASE_SHOWING:
        # 현재 순서의 그룹(최대 2개)이 켜진 상태일 때 표시
        if game.show_lit and game.show_index < len(game.show_groups):
            group = game.show_groups[game.show_index]
            for pos, is_real in group:
                if is_real:
                    led.set_pixel(pos, scale(255), 0, 0)          # 진짜 - 빨간색
                else:
                    led.set_pixel(pos, scale(255), scale(100), 0)  # 가짜 - 주황색
        # 꺼진 상태(show_lit == False)일 때는 clear()된 상태 그대로 (전부 어둠)

    elif game.phase == PHASE_GUESSING:
        # 답을 맞히는 중: 전체를 은은한 보라색으로 (물고기 위치는 숨김)
        p = scale(60)
        for i in range(NUM_LEDS):
            led.set_pixel(i, p, 0, p)

    elif game.phase == PHASE_RESULT:
        if game.last_correct:
            # 정답: 초록색 반짝
            g = scale(255)
            for i in range(NUM_LEDS):
                led.set_pixel(i, 0, g, 0)
        else:
            # 오답: 정답 위치를 빨간색으로 보여줌, 나머지는 어둡게
            for i in range(NUM_LEDS):
                led.set_pixel(i, scale(10), scale(10), scale(10))
            led.set_pixel(game.real_fish_position, scale(255), 0, 0)
            if game.last_answer is not None and 0 <= game.last_answer < NUM_LEDS:
                led.set_pixel(game.last_answer, 0, 0, scale(255))

    led.write()


# ------------------------------------------------------------
# 5. 와이파이 연결
# ------------------------------------------------------------
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    print("와이파이 연결 중", end="")
    timeout = 20
    while not wlan.isconnected() and timeout > 0:
        print(".", end="")
        time.sleep(1)
        timeout -= 1

    if wlan.isconnected():
        ip = wlan.ifconfig()[0]
        print("\n연결 성공! IP 주소:", ip)
        return ip
    else:
        print("\n와이파이 연결 실패")
        return None


# ------------------------------------------------------------
# 6. 웹 대시보드 HTML (물고기 위치는 표시하지 않음 - LED 바만 보고 맞히기)
# ------------------------------------------------------------
def get_html():
    return """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>LED 기억력 낚시 게임</title>
<style>
  body {
    font-family: 'Segoe UI', sans-serif;
    background: linear-gradient(180deg, #1e3c72, #2a5298);
    color: white;
    margin: 0;
    padding: 20px;
    text-align: center;
    min-height: 100vh;
  }
  h1 { font-size: 1.5em; margin-bottom: 5px; }
  #status {
    font-size: 1.1em;
    margin: 12px 0;
    padding: 10px;
    border-radius: 10px;
    background: rgba(255,255,255,0.1);
  }
  #scoreBoard {
    font-size: 1.2em;
    margin-bottom: 10px;
  }

  /* 60칸 그리드 - 위치 정보 없이, 단순히 상태(대기/입력중/결과)만 표시 */
  #ledGrid {
    display: grid;
    grid-template-columns: repeat(10, 1fr);
    gap: 4px;
    max-width: 480px;
    margin: 15px auto;
  }
  .cell {
    aspect-ratio: 1 / 1;
    background: #223;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.7em;
    color: rgba(255,255,255,0.5);
    border: 1px solid rgba(255,255,255,0.15);
  }
  .cell.guessing { background: #6c3fa8; }
  .cell.correct-answer { background: #e74c3c; color: white; }
  .cell.my-answer { background: #3498db; color: white; }
  .cell.win-flash { background: #2ecc71; }

  #answerArea {
    margin-top: 20px;
  }
  #answerInput {
    width: 120px;
    height: 50px;
    font-size: 1.4em;
    text-align: center;
    border-radius: 10px;
    border: none;
    margin-right: 10px;
  }
  #submitButton {
    width: 150px;
    height: 54px;
    font-size: 1.2em;
    background: #2ecc71;
    border: none;
    border-radius: 10px;
    color: white;
    font-weight: bold;
    box-shadow: 0 4px 0 #27ae60;
  }
  #submitButton:active {
    box-shadow: 0 1px 0 #27ae60;
    transform: translateY(3px);
  }
  #submitButton:disabled {
    background: #7f8c8d;
    box-shadow: 0 4px 0 #566;
  }
  #startButton {
    margin-top: 20px;
    width: 90%;
    max-width: 350px;
    height: 60px;
    font-size: 1.2em;
    background: #3498db;
    border: none;
    border-radius: 15px;
    color: white;
    font-weight: bold;
    box-shadow: 0 4px 0 #2980b9;
  }
  #startButton:active {
    box-shadow: 0 1px 0 #2980b9;
    transform: translateY(3px);
  }
</style>
</head>
<body>
  <h1>🎣 LED 기억력 낚시 게임</h1>
  <div id="scoreBoard">점수: <span id="score">0</span> | 라운드: <span id="round">0</span></div>
  <div id="status">시작 버튼을 눌러 게임을 시작하세요</div>

  <div id="ledGrid"></div>

  <div id="answerArea">
    <input type="number" id="answerInput" min="0" max="59" placeholder="0~59">
    <button id="submitButton" disabled>제출</button>
  </div>

  <button id="startButton">게임 시작</button>

<script>
const NUM_LEDS = 60;
let currentPhase = "waiting";

// 60칸 그리드 생성 (번호만 표시, 물고기 위치는 표시하지 않음)
const grid = document.getElementById('ledGrid');
for (let i = 0; i < NUM_LEDS; i++) {
  const cell = document.createElement('div');
  cell.className = 'cell';
  cell.id = 'cell-' + i;
  cell.innerText = i;
  grid.appendChild(cell);
}

document.getElementById('startButton').addEventListener('click', () => {
  fetch('/start');
});

document.getElementById('submitButton').addEventListener('click', () => {
  const val = document.getElementById('answerInput').value;
  if (val === '') return;
  fetch('/answer?value=' + val);
  document.getElementById('submitButton').disabled = true;
});

// 엔터키로도 제출 가능하게
document.getElementById('answerInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    document.getElementById('submitButton').click();
  }
});

function clearGrid() {
  for (let i = 0; i < NUM_LEDS; i++) {
    const cell = document.getElementById('cell-' + i);
    cell.className = 'cell';
  }
}

function updateStatus() {
  fetch('/status')
    .then(res => res.json())
    .then(data => {
      document.getElementById('score').innerText = data.score;
      document.getElementById('round').innerText = data.round_num;
      currentPhase = data.phase;

      clearGrid();

      if (data.phase === 'waiting') {
        document.getElementById('status').innerText = '시작 버튼을 눌러 게임을 시작하세요';
        document.getElementById('submitButton').disabled = true;

      } else if (data.phase === 'showing') {
        // 물고기 위치는 대시보드에 표시하지 않고, LED 바만 보고 기억하도록 함
        document.getElementById('status').innerText = 'LED 바를 잘 보고 진짜 물고기(빨간색) 위치를 기억하세요!';
        document.getElementById('submitButton').disabled = true;

      } else if (data.phase === 'guessing') {
        document.getElementById('status').innerText = '진짜 물고기가 있던 칸 번호를 입력하세요!';
        document.getElementById('submitButton').disabled = false;
        for (let i = 0; i < NUM_LEDS; i++) {
          document.getElementById('cell-' + i).classList.add('guessing');
        }

      } else if (data.phase === 'result') {
        document.getElementById('submitButton').disabled = true;
        if (data.last_correct === true) {
          document.getElementById('status').innerText = '🎉 정답입니다! 점수 획득!';
          for (let i = 0; i < NUM_LEDS; i++) {
            document.getElementById('cell-' + i).classList.add('win-flash');
          }
        } else if (data.last_correct === false) {
          document.getElementById('status').innerText =
            '❌ 오답! 정답은 ' + data.real_position + '번 칸이었어요.';
          document.getElementById('cell-' + data.real_position).classList.add('correct-answer');
          if (data.last_answer !== null) {
            document.getElementById('cell-' + data.last_answer).classList.add('my-answer');
          }
        }
        document.getElementById('answerInput').value = '';
      }
    })
    .catch(err => console.log(err));
}

setInterval(updateStatus, 200);
</script>
</body>
</html>
"""


# ------------------------------------------------------------
# 7. 웹 서버 (논블로킹 처리)
# ------------------------------------------------------------
def start_server(ip):
    addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(2)
    s.settimeout(0.01)  # 논블로킹처럼 동작하도록 짧은 타임아웃
    print("웹 서버 시작됨! 스마트폰에서 아래 주소로 접속하세요:")
    print("http://{}/".format(ip))
    return s


def handle_client(cl):
    try:
        cl.settimeout(1.0)
        request = cl.recv(1024).decode("utf-8")

        # 요청 첫 줄에서 경로 추출
        first_line = request.split("\r\n")[0]
        path = first_line.split(" ")[1]

        if path.startswith("/status"):
            # showing 단계에서도 위치 정보를 절대 보내지 않음 (LED로만 확인 가능하도록)
            if game.phase == PHASE_RESULT:
                real_position_val = game.real_fish_position
            else:
                real_position_val = -1

            if game.last_correct is None:
                last_correct_str = "null"
            else:
                last_correct_str = "true" if game.last_correct else "false"

            last_answer_str = "null" if game.last_answer is None else str(game.last_answer)

            body = (
                '{{"phase": "{}", "score": {}, "round_num": {}, '
                '"real_position": {}, '
                '"last_correct": {}, "last_answer": {}}}'
            ).format(
                game.phase,
                game.score,
                game.round_num,
                real_position_val,
                last_correct_str,
                last_answer_str,
            )
            response = (
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n\r\n" + body
            )
        elif path.startswith("/answer"):
            try:
                value_str = path.split("value=")[1].split("&")[0]
                answer = int(value_str)
            except (IndexError, ValueError):
                answer = -1
            game.submit_answer(answer)
            response = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nOK"
        elif path.startswith("/start"):
            game.score = 0
            game.round_num = 0
            game.start_new_round()
            response = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nSTARTED"
        else:
            html = get_html()
            response = (
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
                + html
            )

        cl.send(response)
    except Exception as e:
        print("클라이언트 처리 오류:", e)
    finally:
        cl.close()


# ------------------------------------------------------------
# 8. 메인 루프
# ------------------------------------------------------------
def main():
    ip = connect_wifi()
    if ip is None:
        print("와이파이 연결 실패로 게임을 시작할 수 없습니다.")
        return

    server = start_server(ip)
    render()  # 초기 대기 화면

    last_render = time.ticks_ms()
    RENDER_INTERVAL = 30  # ms (순차 점등이 정확한 타이밍에 반영되도록 짧게)

    while True:
        # 웹 클라이언트 요청 처리 (있으면)
        try:
            cl, addr = server.accept()
            handle_client(cl)
        except OSError:
            pass  # 타임아웃 -> 요청 없음

        # 게임 단계 전환 체크
        prev_phase = game.phase
        prev_lit = game.show_lit
        prev_index = game.show_index
        game_step()

        # 단계, 점등 상태, 순서 인덱스 중 하나라도 바뀌었거나 일정 주기가 지나면 LED 다시 그리기
        now = time.ticks_ms()
        changed = (
            prev_phase != game.phase
            or prev_lit != game.show_lit
            or prev_index != game.show_index
        )
        if changed or time.ticks_diff(now, last_render) >= RENDER_INTERVAL:
            render()
            last_render = now


if __name__ == "__main__":
    main()

