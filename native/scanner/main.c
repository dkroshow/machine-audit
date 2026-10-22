/* A fixed scanner entry point. Python stays in this process so macOS can
 * attribute protected reads to this executable, subject to trial validation. */
#include <Python.h>
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#ifndef MACHINE_AUDIT_PYTHON_HOME
#error "Build with an explicit Python home"
#endif

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--identity") == 0) {
        puts("local.machine-audit.scanner; embedded Python; no collection");
        return 0;
    }
    int check_only = argc == 2 && strcmp(argv[1], "--check-access") == 0;
    if ((argc != 1 && !check_only) || getuid() == 0 || geteuid() != getuid() || getppid() != 1) {
        fputs("Start the installed scanner through machine-audit.\n", stderr);
        return 2;
    }
    char executable[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0) return 2;
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.site_import = 0;
    config.parse_argv = 0;
    config.install_signal_handlers = 1;
    PyStatus status = PyConfig_SetBytesString(&config, &config.home, MACHINE_AUDIT_PYTHON_HOME);
    if (!PyStatus_Exception(status)) status = PyConfig_SetBytesString(&config, &config.program_name, executable);
    if (!PyStatus_Exception(status)) status = PyConfig_SetBytesArgv(&config, argc, argv);
    if (!PyStatus_Exception(status)) status = PyConfig_SetString(&config, &config.run_command,
        L"import json, pathlib, sys\n"
        L"root = pathlib.Path(sys.executable).resolve().parents[3]\n"
        L"with (root / 'current.json').open() as f: install = json.load(f)\n"
        L"sys.path.insert(0, install['release'])\n"
        L"from machine_audit.scanner_entry import main\n"
        L"raise SystemExit(main(install, check_only=sys.argv[1:] == ['--check-access']))\n");
    if (!PyStatus_Exception(status)) status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        fputs("Scanner runtime initialization failed.\n", stderr);
        return 2;
    }
    return Py_RunMain();
}
