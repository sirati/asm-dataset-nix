#include <sys/ptrace.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(void) {
    pid_t child = fork();
    int status;
    if (child < 0) return 1;
    if (child == 0) {
        if (ptrace(PTRACE_TRACEME, 0, 0, 0) < 0) _exit(40);
        raise(SIGSTOP);
        _exit(23);
    }
    if (waitpid(child, &status, 0) != child || !WIFSTOPPED(status)) return 2;
    if (ptrace(PTRACE_SETOPTIONS, child, 0,
               PTRACE_O_EXITKILL | PTRACE_O_TRACEEXEC | PTRACE_O_TRACEFORK | PTRACE_O_TRACECLONE) < 0) return 3;
    if (ptrace(PTRACE_CONT, child, 0, 0) < 0) return 4;
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status) != 23) return 5;
    puts("{\"schema\":1,\"ptrace_allowed\":true,\"child_status\":23}");
    return 0;
}
