@echo off
call C:\Users\Tsolis\AppData\Local\miniconda3\Scripts\activate.bat cad-reconstructor
python C:\Users\Tsolis\Documents\GitRepos\Solidworks-Part-Generator\debug_quick.py > C:\Users\Tsolis\AppData\Local\Temp\debug_out.txt 2>&1
echo Python exit code: %ERRORLEVEL% >> C:\Users\Tsolis\AppData\Local\Temp\debug_out.txt
