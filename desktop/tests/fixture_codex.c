#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <direct.h>

int main(int argc, char** argv) {
    /* The service must consume the launch secret before creating runtime children. */
    if (getenv("CODEX_OFFICE_LAUNCH_TOKEN")) return 9;
    if (argc > 1 && strcmp(argv[1], "--version") == 0) {
        printf("codex-cli 0.147.0\n");
        return 0;
    }
    char line[4096];
    char cwd[4096], escaped_cwd[8192];
    if (!_getcwd(cwd, sizeof(cwd))) return 10;
    strcat_s(cwd, sizeof(cwd), "\\workspaces\\developer");
    size_t offset = 0;
    for (size_t i = 0; cwd[i]; ++i) {
        if (cwd[i] == '\\' || cwd[i] == '"') escaped_cwd[offset++] = '\\';
        escaped_cwd[offset++] = cwd[i];
    }
    escaped_cwd[offset] = 0;
    while (fgets(line, sizeof(line), stdin)) {
        char* id_pos = strstr(line, "\"id\":");
        if (id_pos) {
            long req_id = 0;
            if (sscanf(id_pos, "\"id\": %ld", &req_id) != 1) {
                sscanf(id_pos, "\"id\":%ld", &req_id);
            }
            if (strstr(line, "\"initialize\"")) {
                printf("{\"id\":%ld,\"result\":{\"serverInfo\":{\"name\":\"fixture\",\"version\":\"0.1.0\"}}}\n", req_id);
                fflush(stdout);
            } else if (strstr(line, "\"model/list\"")) {
                printf("{\"id\":%ld,\"result\":{\"data\":[{\"model\":\"gpt-6.1-sol\"}],\"nextCursor\":null}}\n", req_id);
                fflush(stdout);
            } else if (strstr(line, "\"account/read\"")) {
                printf("{\"id\":%ld,\"result\":{\"account\":null,\"requiresOpenaiAuth\":false}}\n", req_id);
                fflush(stdout);
            } else if (strstr(line, "\"config/read\"")) {
                printf("{\"id\":%ld,\"result\":{\"config\":{\"model\":\"gpt-6.1-sol\",\"model_provider\":\"openai\",\"mcp_servers\":{}}}}\n", req_id);
                fflush(stdout);
            } else if (strstr(line, "\"thread/start\"") || strstr(line, "\"thread/resume\"")) {
                printf("{\"id\":%ld,\"result\":{\"thread\":{\"id\":\"fixture-thread-1\"},\"sandbox\":{\"type\":\"readOnly\",\"networkAccess\":0},\"approvalPolicy\":\"never\"}}\n", req_id);
                fflush(stdout);
            } else if (strstr(line, "\"thread/read\"")) {
                printf("{\"id\":%ld,\"result\":{\"thread\":{\"id\":\"fixture-thread-1\",\"cwd\":\"%s\",\"turns\":[]}}}\n", req_id, escaped_cwd);
                fflush(stdout);
            } else {
                printf("{\"id\":%ld,\"result\":{}}\n", req_id);
                fflush(stdout);
            }
        }
    }
    return 0;
}
