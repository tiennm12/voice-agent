TÀI LIỆU YÊU CẦU SẢN PHẨM (PRD): SMART VOICEMAIL AGENT
1. TỔNG QUAN DỰ ÁN
Hệ thống là một trợ lý AI thông minh tự động thay mặt chủ thuê bao nghe điện thoại khi họ không thể nhấc máy (bận, ngoài vùng phủ sóng, cố tình từ chối). Agent sẽ giao tiếp tự nhiên với người gọi để nhận lời nhắn, sau đó tổng hợp thông tin và gửi ngay cho chủ thuê bao. Hệ thống đi kèm một giao diện Web (Dashboard) để chủ thuê bao quản lý.

2. LUỒNG NGHIỆP VỤ CỐT LÕI (USER FLOW)
Kích hoạt: Có người gọi đến số điện thoại của chủ thuê bao nhưng không được phản hồi sau X hồi chuông (hoặc chủ thuê bao bấm từ chối). Cuộc gọi được chuyển tiếp (Call Forwarding) tới hệ thống SIP/Tổng đài của Agent.
Giao tiếp (Voice Agent):
Chào hỏi: "Chào bạn, [Tên chủ máy] hiện không thể nghe máy. Mình là trợ lý AI, bạn cần nhắn gì không ạ?"
Lắng nghe & Phản hồi: Thu thập thông tin, hỏi thêm chi tiết nếu cần (VD: "Cho mình xin tên của bạn và việc cần nhắn nhé").
Xác nhận & Cảm ơn: "Mình đã ghi nhận nội dung... Cảm ơn bạn, mình sẽ báo lại ngay."
Xử lý sau cuộc gọi: AI tạo bản tóm tắt (Summary) nội dung cuộc gọi.
Thông báo: Hệ thống gửi tin nhắn (SMS/Zalo/Telegram) chứa nội dung tóm tắt đến thiết bị của chủ thuê bao.
Lưu trữ: Toàn bộ file ghi âm, transcript (văn bản) và summary được đưa lên Web Dashboard.
3. YÊU CẦU CHỨC NĂNG (FUNCTIONAL REQUIREMENTS)
3.1. Voice Agent (Module Nghe Gọi)
Cần tích hợp thêm một tổng đài ảo (SIP Trunk) để nối với mạng viễn thông thật (thay vì chỉ dùng Browser mic như hiện tại).

Tích hợp SIP/VoIP: Nối Node.js backend với các nhà cung cấp như asterik sip trunk để nhận luồng âm thanh cuộc gọi thật.
Trong giai đoạn đầu sẽ là POC bằng 1 kết nối websocket đến 1 tổng đài ảo đã.
Kịch bản hội thoại (Prompting):
Tự động phát hiện khi nào người gọi nói xong để phản hồi.
Hỗ trợ ngắt lời (Barge-in): Nếu người gọi ngắt lời Agent, Agent phải dừng nói ngay lập tức (đã có trong source code).
Auto-Hangup: Nhận diện khi câu chuyện kết thúc hoặc khi người gọi im lặng quá lâu để tự động cúp máy (kết thúc WebSocket/API session).
3.2. Web Dashboard (Dành cho Chủ thuê bao)
Hệ thống quản lý trên web dành cho người dùng cuối.

Quản lý cuộc gọi nhỡ (Inbox):
Hiển thị danh sách cuộc gọi: Số điện thoại gọi đến, Thời gian, Trạng thái (đã đọc/chưa đọc).
Xem chi tiết: Hiển thị Tóm tắt (Summary), Bản dịch chi tiết cuộc hội thoại (Transcript) dạng tin nhắn chat.
Nghe lại: Trình phát Audio (Audio Player) để nghe lại file ghi âm gốc (hoặc nghe giọng của Agent).
Quản lý Kịch bản (Settings):
Cho phép người dùng tuỳ chỉnh câu chào (Greeting).
Cho phép người dùng cấu hình "System Prompt" (thay vì fix cứng trong config/systemDoc.md như hiện tại). VD: "Hãy đóng vai trợ lý của Giám đốc tên là Tuấn..."
Thống kê (Analytics): Thống kê số lượng cuộc gọi nhỡ theo ngày, tuần, tháng.
3.3. Hệ thống Thông báo (Notification Engine)
Sau khi webhook cuộc gọi kết thúc, trigger hàm gọi LLM (GPT-4o-mini) đọc file transcript và tóm tắt theo cấu trúc: [Tên người gọi] + [Nội dung tóm tắt] + [Độ khẩn cấp].
Tích hợp API (SMS, Zalo OA, hoặc Telegram Bot) để bắn thông báo ngay lập tức vào điện thoại chủ nhân.
4. CÁC THÀNH PHẦN ĐỀ XUẤT THÊM (ĐỂ SẢN PHẨM HOÀN THIỆN & THƯƠNG MẠI HÓA)
Nếu bạn muốn biến project này thành một sản phẩm SaaS (Software as a Service) hoàn chỉnh, bạn sẽ cần thêm:

Phân loại Spam/Cuộc gọi rác (Spam Detection & Auto-block):
Nếu AI nhận diện cuộc gọi là telesales, lừa đảo, hoặc robocall, nó sẽ tự động tóm tắt là "Spam" và không gửi tin nhắn làm phiền chủ nhân. Hoặc agent có thể được cấu hình để "troll" lại spammer.
Lịch làm việc (Working Hours / Out of office):
Cho phép cấu hình để Agent trả lời khác nhau tùy thời gian. VD: Buổi tối sẽ nói "Công ty đã hết giờ làm việc, vui lòng để lại lời nhắn"; trong lúc họp sẽ nói "Sếp tôi đang họp...".
Phân loại độ khẩn cấp (Urgency Categorization):
AI tự phân tích ngữ nghĩa: Nếu người gọi có vẻ rất gấp (VD: tai nạn, chốt hợp đồng lớn), hệ thống sẽ gán tag [🔴 KHẨN CẤP] và gọi điện thoại trực tiếp cho chủ nhân (thay vì chỉ nhắn tin).
Đa giọng nói & Ngôn ngữ (Voice & Language Settings):
Tận dụng ElevenLabs để cho phép chủ thuê bao chọn giọng nam/nữ, giọng Bắc/Nam, hoặc tự clone chính giọng của chủ thuê bao để Agent nghe y hệt họ.
Auto-detect ngôn ngữ: Nếu khách nước ngoài gọi, Agent tự động chuyển sang nói tiếng Anh.
Kiến trúc Multi-tenant (SaaS Backend):
Tách biệt dữ liệu. Cấu trúc DB (PostgreSQL/MongoDB) để mỗi User có users, calls, settings, billing (Quản lý credit thời lượng nghe gọi).
Mobile App / Telegram Mini App:
Giao diện Web là chưa đủ cho cuộc gọi nhỡ. Cần một Telegram Bot hoặc Zalo Mini App để người dùng nhận Push notification và nghe lại ghi âm ngay trên điện thoại mà không cần đăng nhập Web.
5. YÊU CẦU PHI CHỨC NĂNG (NFR)
Độ trễ (Latency): Phải giữ được Time-to-first-audio (TTFA) < 1 giây như bản hiện tại để cuộc gọi điện thoại không bị cảm giác "giả".
Lưu trữ & Bảo mật: File ghi âm (Audio) phải được lưu trữ an toàn (AWS S3) và có thời hạn tự động xóa (Retention policy) sau 30-60 ngày để bảo vệ quyền riêng tư (Privacy).