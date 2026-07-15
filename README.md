# Công cụ đo hiệu quả Prompt Cache của Azure OpenAI

Đây là **benchmark** nội bộ độc lập, tức chương trình tạo một khối lượng yêu cầu có kiểm soát để đo và so sánh, dành cho **Azure OpenAI Responses API v1**. Responses API là giao diện lập trình ứng dụng dùng để gửi dữ liệu đầu vào và nhận nội dung do mô hình tạo ra; `v1` là phiên bản đường dẫn API. Mô hình triển khai mặc định là `gpt-5.4-mini`.

Công cụ chỉ đánh giá hiệu quả **prompt cache**. Prompt cache là cơ chế Azure OpenAI tái sử dụng phép tính cho phần đầu giống hệt nhau của nhiều prompt. Prompt là toàn bộ chỉ dẫn và dữ liệu đầu vào gửi cho mô hình. Cơ chế này khác **response cache**: hệ thống không lưu nguyên câu trả lời cũ; phần dữ liệu thay đổi vẫn được xử lý và mô hình vẫn tạo câu trả lời mới.

Phạm vi đo gồm:

- số token đầu vào được tái sử dụng, đọc từ `usage.input_tokens_details.cached_tokens`;
- thời gian phản hồi khi nhận dữ liệu theo luồng: đến sự kiện đầu tiên, đến chữ đầu tiên, đến chữ cuối cùng (TTLT) và khoảng ngắt giữa các đoạn chữ (TBT);
- giá công khai hiện hành từ Azure Retail Prices API cho token đầu vào thường, token đầu vào được cache và token đầu ra;
- so sánh yêu cầu cold/warm và A/B để cô lập ảnh hưởng của phần đầu prompt, công cụ, schema, cache key và nhịp gửi yêu cầu;
- mười cặp đo độ trễ cold/warm, mỗi cặp dùng namespace riêng;
- báo cáo có thể tái tạo về cache, chi phí và độ tin cậy.

## Bạn nhận được gì từ một lần chạy

Mỗi lần chạy tạo `runs/<run-id>/report.md` trả lời bốn câu hỏi:

1. **Endpoint của bạn có cache thật không?** Đọc trực tiếp từ `cached_tokens` do dịch vụ trả về, không suy diễn từ tốc độ phản hồi.
2. **Cấu hình đúng thì tiết kiệm bao nhiêu?** Run mẫu: nhóm cấu hình đúng đạt 96.8% token được cache, giảm 87.1% chi phí input và 80.6% tổng chi phí.
3. **Nếu cache kém thì vì cái gì?** Benchmark cố ý phá cache theo bốn cách (phần đầu prompt bất ổn, tool schema đổi, cache key sai, gửi dồn dập) để chỉ đích danh nguyên nhân và việc cần sửa trong hệ thống của bạn.
4. **Cache có làm nhanh hơn không?** Đo bằng mười cặp yêu cầu cold/warm giống hệt từng byte; báo cáo chỉ kết luận khi số đo đủ chuẩn.

Cách đọc từng con số và quy đổi mức tiết kiệm sang lưu lượng thật của bạn: [PLAYBOOK, phần A](docs/PLAYBOOK_VI.md). Báo cáo mẫu từ một lần chạy thật: [examples/sample-report.md](examples/sample-report.md).

## Cài đặt

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
```

Chương trình chỉ đọc hai **biến môi trường**, tức cặp tên/giá trị cấu hình được cung cấp cho tiến trình, từ `.env`:

```dotenv
OPENAI_BASE_URL="https://<resource>.openai.azure.com/openai/v1/"
OPENAI_API_KEY=""
```

`OPENAI_BASE_URL` là địa chỉ gốc của Azure OpenAI Responses API v1 — bắt buộc HTTPS, không chấp nhận query/fragment hay URL kiểu deployment. `OPENAI_API_KEY` là khóa xác thực; không ghi khóa thật vào Git. Quy tắc URL đầy đủ và các lỗi cấu hình thường gặp: [PLAYBOOK, phần B.2](docs/PLAYBOOK_VI.md).

Sau khi cài có ba entrypoint tương đương:

```bash
.venv/bin/azure-openai-cache-benchmark --help
.venv/bin/python -m azure_openai_cache_benchmark --help
.venv/bin/python benchmark.py --help
```

Console script và `python -m` lấy `.env` và `runs/` mặc định theo thư mục làm việc hiện tại; launcher `benchmark.py` luôn theo thư mục repository. Các flag `--env-file` và `--output-root` ghi đè được.

## Bắt đầu nhanh

```bash
# 1. Kiểm thử offline — không gọi mạng
.venv/bin/python -m unittest discover -s tests

# 2. Dry-run — validate 102 yêu cầu + trần chi phí, không gọi mô hình
.venv/bin/azure-openai-cache-benchmark dry-run

# 3. Chạy thật — phát sinh chi phí; duyệt trần chi phí ở bước 2 trước
.venv/bin/azure-openai-cache-benchmark live --confirm-live

# 4. Đọc kết quả
open runs/<run-id>/report.md
```

Model khác `gpt-5.4-mini` cần `--skip-pricing` hoặc ghi đè đồng thời cả ba mức giá. Quy trình đầy đủ từng bước — bảng flag, probe, xử lý lỗi, tạo lại báo cáo ngoại tuyến, xuất mẫu chia sẻ an toàn — nằm trong [PLAYBOOK, phần B](docs/PLAYBOOK_VI.md).

## Đọc gì ở đâu

| Nhu cầu | Tài liệu |
| --- | --- |
| Chạy benchmark với endpoint thật từng bước; xử lý lỗi | [PLAYBOOK — phần B](docs/PLAYBOOK_VI.md) |
| Đọc và diễn giải báo cáo; schema tệp kết quả; bẫy đọc sai | [PLAYBOOK — phần A](docs/PLAYBOOK_VI.md) |
| Test với system prompt riêng của bạn | [PLAYBOOK — phần C](docs/PLAYBOOK_VI.md) |
| Cải thiện cache efficiency cho hệ thống agent | [PLAYBOOK — phần D](docs/PLAYBOOK_VI.md) và [GUIDE mục 15–21](GUIDE_VI.md) |
| Cơ chế prompt cache; thiết kế suite; công thức chỉ số; acceptance; giới hạn | [GUIDE_VI.md](GUIDE_VI.md) |
| Số liệu mẫu thật đã làm sạch | [examples/sample-report.md](examples/sample-report.md) |
| Tài liệu chính thức của Microsoft | mục "Nguồn chính thức" cuối [GUIDE_VI.md](GUIDE_VI.md) |
