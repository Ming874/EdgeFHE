@echo off
rem Vivado 2022.1 system build of V8b rev5 (no board access).
rem 1) hardware\hls: vitis_hls -f run_v8b_export.tcl   2) this script.
rem usage: run_system_rev5.bat [IP_REPO] [BUILD_DIR]   (VIVADO_BIN defaults to C:\Xilinx\Vivado\2022.1\bin)
setlocal
set "HERE=%~dp0"
set "IPREPO=%~1"
if "%IPREPO%"=="" set "IPREPO=%HERE%..\hls\v8b_ip_prj\sol200\impl\ip"
set "BUILD=%~2"
if "%BUILD%"=="" set "BUILD=%HERE%build_rev5"
if "%VIVADO_BIN%"=="" set "VIVADO_BIN=C:\Xilinx\Vivado\2022.1\bin"
set "TCL=%HERE%build_v8b_system_v3.tcl"
set "TCL=%TCL:\=/%"
set "IPREPO=%IPREPO:\=/%"
set "BUILD=%BUILD:\=/%"
set "SRC=%HERE%sources"
set "SRC=%SRC:\=/%"
call "%VIVADO_BIN%\vivado.bat" -mode batch -nojournal -source "%TCL%" -tclargs "%BUILD%" "%IPREPO%" "%SRC%"
exit /b %ERRORLEVEL%
