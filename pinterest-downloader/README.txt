PINTEREST DOWNLOADER — macOS / Python 3.10+

1. Giải nén thư mục.
2. Trong Terminal chạy: zsh setup.command
3. Chạy: zsh pweb
4. Mở http://127.0.0.1:8778

Dán nhiều link pin hoặc board từ pinterest.com hoặc pin.it.
Chọn ảnh, video hoặc cả hai. Nhập tên thư mục nếu muốn đặt tên; để trống, công cụ tự tạo thư mục riêng dạng:
~/Downloads/Pinterest/YYYY-MM-DD_HH-MM-SS_mã/
Các ảnh/video của toàn bộ danh sách được lưu trực tiếp trong thư mục đó, không tạo thư mục con cho từng link.
Tối đa 4 link chạy cùng lúc; các link trong cùng danh sách được xử lý lần lượt để ghi nhận đúng tệp.
Tool tải bản media có chất lượng cao nhất được extractor Pinterest cung cấp. Có thể bật “Upscale 2×” để tạo thêm bản phóng lớn cạnh bản gốc; cần cài ffmpeg. Upscale dùng nội suy Lanczos, tăng kích thước ảnh/video nhưng không khôi phục chi tiết đã mất.
Có trạng thái, lỗi, liên kết xem/lưu tệp và nút mở thư mục.
Không chọn đăng nhập nếu không cần. Tùy chọn trình duyệt dùng cookie Pinterest cục bộ.

Công cụ dùng gallery-dl và yt-dlp. Hỗ trợ thực tế tùy Pinterest và phiên bản extractor.
Chưa kiểm thử tải thật trong môi trường mạng hạn chế của phiên tạo tool.
Nếu Pinterest thay đổi: .venv/bin/python -m pip install -U gallery-dl yt-dlp
Ctrl+C để dừng. Không dừng khi đang tải.
Chỉ tải nội dung bạn được phép sử dụng.
