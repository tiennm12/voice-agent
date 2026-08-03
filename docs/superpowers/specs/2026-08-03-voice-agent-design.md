# Voice Agent (Vietnamese) — Design

Date: 2026-08-03
Status: Approved

## Goal

Voice agent thực hiện "cuộc gọi ảo" bằng tiếng Việt. Người dùng nói qua mic trên
browser, agent trả lời lại bằng giọng nói tiếng Việt. Ưu tiên số 1: latency thấp.

## Scope

- Prototype: 1 cuộc gọi tại 1 thời điểm, không cần multi-tenant/scale.
- Turn detection: voice activity detection tự động (không push-to-talk).
- Barge-in: user nói đè lúc agent đang nói phải cắt audio ngay.
- Document context: 1 tài liệu ngắn nhúng tĩnh vào system prompt (placeholder,
  nội dung thật điền sau).

## Architecture

```
Browser  <---WebSocket--->  Node.js backend  <---WS--->  OpenAI Realtime API (STT only, server VAD)
                                    |
                                    +---REST(stream)--->  GPT-4.1-mini
                                    |
                                    +---WS(stream)----->  ElevenLabs (Flash v2.5 TTS)
```

Backend là 1 process Node.js, đóng vai trò relay/orchestrator duy nhất. Mỗi
cuộc gọi = 1 WebSocket connection browser↔backend, giữ song song 1 kết nối
persistent tới OpenAI Realtime (STT) và mở kết nối ElevenLabs theo từng turn.

Không dùng OpenAI Realtime API để tự sinh response — chỉ dùng cho
speech-to-text (transcription intent + server-side VAD). Việc sinh response
tách riêng qua GPT-4.1-mini để kiểm soát được system prompt/document và cách
chunk text gửi sang TTS.

## Components

### Frontend (browser)

- Capture mic qua AudioWorklet, encode PCM16 16kHz mono, gửi frame ~20-40ms
  qua WebSocket tới backend.
- Playback: nhận audio chunk qua WebSocket, queue vào AudioContext, phát nối
  tiếp không khoảng trống (gapless).
- Nhận event `interrupt` từ backend → flush queue phát, dừng ngay (xử lý
  barge-in).
- UI tối thiểu: nút Start/End call, trạng thái cuộc gọi hiện tại, transcript
  hiển thị phục vụ debug.

### Backend (Node.js)

Session = 1 WebSocket connection + state machine:
`idle → listening → thinking → speaking → (interrupt) → listening`.

- **Realtime STT client** — WS persistent tới OpenAI Realtime API, forward
  audio frame từ browser. Lắng nghe:
  - `input_audio_buffer.speech_started` — user bắt đầu nói → nếu backend
    đang ở state `speaking`, kích hoạt barge-in (abort turn hiện tại).
  - `conversation.item.input_audio_transcription.completed` — transcript
    cuối cùng sẵn sàng → trigger 1 turn LLM mới.
- **LLM client** — gọi GPT-4.1-mini chat completion với `stream: true`.
  System prompt = instructions cố định + nội dung document ngắn (nhúng từ
  config) + lịch sử hội thoại + transcript mới. Token stream về được gom
  thành chunk tại ranh giới dấu câu kết câu/mệnh đề (`. ! ? ; :` hoặc dấu
  phẩy `,`); nếu vượt ~8-10 từ mà chưa gặp dấu câu nào, flush chunk theo
  fallback để tránh trễ.
- **TTS client** — mỗi chunk text gửi ngay qua ElevenLabs WebSocket
  streaming-input API, model `eleven_flash_v2_5`, giọng tiếng Việt. Audio
  chunk trả về được forward thẳng cho frontend ngay khi nhận, không buffer
  toàn bộ response trước khi gửi.
- **Abort controller** — khi barge-in kích hoạt: abort LLM stream đang chạy
  (AbortController), đóng/hủy ElevenLabs stream của turn hiện tại, gửi
  `interrupt` cho frontend.
- **Config** — document ngắn tách file riêng (`config/systemDoc.md`,
  placeholder), load 1 lần lúc khởi động, nhúng vào template system prompt.

### External services

| Service | Vai trò | Ghi chú |
|---|---|---|
| OpenAI Realtime API | STT + server-side VAD | Chỉ dùng transcription, không dùng response generation của Realtime |
| GPT-4.1-mini | Sinh response text | Streaming, system prompt gồm document ngắn |
| ElevenLabs | TTS tiếng Việt | Model `eleven_flash_v2_5`, streaming input/output qua WS |

## Data flow (happy path, 1 turn)

1. User nói → browser stream PCM frame qua WS → backend forward tới OpenAI
   Realtime.
2. Server VAD phát hiện hết câu → event transcript final.
3. Backend gọi GPT-4.1-mini streaming (system prompt + document + lịch sử +
   transcript mới).
4. Token về tới đâu, gom chunk theo dấu câu → flush ngay từng chunk sang
   ElevenLabs WS.
5. Audio ElevenLabs trả về từng phần → forward thẳng browser → phát ngay khi
   nhận (không chờ đủ câu/đủ response).
6. Nếu trong lúc bước 3-5 đang chạy, OpenAI Realtime báo `speech_started`
   (user nói đè) → abort LLM stream + ElevenLabs turn hiện tại, gửi
   `interrupt` cho frontend để flush playback, quay lại bước 1.

## Error handling

- **OpenAI Realtime WS rớt kết nối**: retry 1 lần; thất bại thì kết thúc
  call, hiện banner lỗi cho user (không cố TTS lời xin lỗi vì pipeline STT có
  thể đang hỏng đồng bộ với hạ tầng).
- **GPT-4.1-mini lỗi/timeout**: câu xin lỗi cố định (hardcoded fallback text)
  đưa qua TTS bình thường ("Xin lỗi, tôi gặp sự cố kỹ thuật, bạn nói lại được
  không?"), log lỗi kèm session id, giữ session sống để thử turn tiếp theo.
- **ElevenLabs lỗi giữa chừng 1 turn**: abort turn đó; nếu bản thân ElevenLabs
  down (không TTS được câu xin lỗi), hiện banner text lỗi ở frontend thay vì
  cố phát audio.
- **Browser đóng WS**: backend dọn toàn bộ kết nối downstream cho session đó
  (đóng WS Realtime, đóng WS ElevenLabs nếu còn mở), giải phóng tài nguyên.
- Lỗi log server-side kèm session id + timestamp (console log đủ dùng ở scope
  prototype 1-call, chưa cần hệ thống monitoring ngoài).

## Testing

- Scope hiện tại (1 call/lúc, prototype) nên kiểm thử chính là manual
  end-to-end qua browser thật (mic thật, phát thật).
- Unit test (Node), mock toàn bộ external service qua interface (không gọi
  API thật trong test tự động):
  - Hàm chunk token theo dấu câu + fallback theo số từ.
  - Builder ghép system prompt (instructions + document).
  - State machine transition (idle/listening/thinking/speaking/interrupt),
    đặc biệt case abort giữa chừng khi đang `speaking`.
- Đo latency thủ công bằng timestamp log tại từng mốc: hết audio user nói →
  transcript sẵn sàng → token đầu tiên từ LLM → chunk audio đầu tiên từ TTS →
  audio đầu tiên phát ra ở browser. Dùng để tìm bottleneck trong quá trình
  phát triển, không phải automated test.

## Out of scope (not building now)

- Multi-call concurrency / session scaling.
- Push-to-talk fallback mode.
- Persisting conversation history across calls.
- Automated audio-quality or latency regression testing.
