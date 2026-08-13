# skills


spring-crud-extractor /path/to/repo interfaces.yaml                 # 结果 YAML 打印到终端
spring-crud-extractor /path/to/repo interfaces.yaml --out report.yaml   # 生成 YAML 报告文件
spring-crud-extractor /path/to/repo --auto --out report.yaml        # 自动发现接口，也出 YAML


interfaces:
  - id: create-user
    http_method: POST
    path: /api/users
    controller: UserController
    controller_method: createUser

  - controller_method: findUser      # 只给方法名，自动在所有 Controller/Service 里搜

  - method_name: deleteUser          # 等价写法

  - service: UserService             # 直接从 Service 开始追踪
    service_method: resetPassword
