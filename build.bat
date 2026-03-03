@echo off
setlocal
cls

:: ============================================================================
:: Универсальный лаунчер GeminiTranslator
:: Сгенерировано: build_master.py (v15.0 - "The Universal Collector")
:: ============================================================================

:: --- Этап 0: Выбор Python (venv/.venv приоритетнее системного) ---
set "PYTHON_EXE=python"
if exist "%~dp0venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"
    echo [+] Найдено виртуальное окружение: venv
) else if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
    echo [+] Найдено виртуальное окружение: .venv
) else (
    echo [!] venv/.venv не найден. Будет использован системный Python.
)
echo [+] Python: "%PYTHON_EXE%"
echo.

:: --- Этап 1: Проверка и запрос прав администратора (если нужно) ---
>nul 2>&1 net session
if '%errorlevel%' NEQ '0' (
    echo.
    echo [+] Запрос прав администратора...
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
    echo UAC.ShellExecute "%~f0", "", "", "runas", 1 >> "%temp%\getadmin.vbs"
    cscript "%temp%\getadmin.vbs" & exit /B
)
if exist "%temp%\getadmin.vbs" ( del "%temp%\getadmin.vbs" )

:: --- Этап 2: Настройка рабочего окружения ---
cd /d "%~dp0"
echo [+] Рабочая директория: %cd%

for %%I in ("%cd%") do set "AppName=%%~nxI"
echo [+] Имя приложения будет: %AppName%
echo.

:: --- Главное меню ---
:menu
cls
echo ======================================================
echo   Универсальный лаунчер для: %AppName%
echo ======================================================
echo.
echo   1. Установить / Обновить зависимости программы
echo.
echo   2. Собрать приложение
echo.
echo   3. Выход
echo.
echo ======================================================
set /p choice="Выберите действие (1, 2 или 3): "

if not defined choice ( goto menu )
if "%choice%"=="1" ( goto install_deps )
if "%choice%"=="2" ( goto build_menu )
if "%choice%"=="3" ( goto :eof )

echo Неверный выбор. Пожалуйста, введите 1, 2 или 3.
pause
goto menu


:: --- Меню сборки ---
:build_menu
cls
echo ======================================================
echo   Выберите тип сборки
echo ======================================================
echo.
echo   1. ПОЛНОСТЬЮ ПОРТАТИВНАЯ (один .exe файл)
echo      - Создает один .exe файл. Все встроено внутрь.
echo      - Легко распространять, но настройки менять нельзя.
echo      - Рекомендуется для большинства пользователей.
echo.
echo   2. ГИБРИДНАЯ (один .exe + папки с данными)
echo      - Создает один .exe и рядом с ним папки с данными.
echo      - Сочетает портативность и возможность менять конфиги.
echo      - Рекомендуется для опытных пользователей.
echo.
echo   3. ПРОДВИНУТАЯ (папка с файлами)
echo      - Создает папку с .exe и всеми зависимостями.
echo      - Позволяет вручную редактировать конфиги и данные.
echo      - Для разработчиков и отладки.
echo.
echo   4. Назад в главное меню
echo.
echo ======================================================
set /p build_choice="Выберите действие (1, 2, 3 или 4): "

if not defined build_choice ( goto build_menu )
if "%build_choice%"=="1" ( goto build_full_portable )
if "%build_choice%"=="2" ( goto build_hybrid )
if "%build_choice%"=="3" ( goto build_advanced )
if "%build_choice%"=="4" ( goto menu )

echo Неверный выбор.
pause
goto build_menu


:: --- Блок установки зависимостей ---
:install_deps
cls
echo --- Установка / обновление зависимостей программы ---
echo.
echo [+] Запуск установки из файла 'requirements.txt'...
"%PYTHON_EXE%" -m pip install --upgrade pip
"%PYTHON_EXE%" -m pip install --upgrade -r "requirements.txt"
if %ERRORLEVEL% NEQ 0 (
    echo [!!!] Ошибка при установке. Проверьте подключение к интернету.
) else (
    echo [OK] Все зависимости успешно установлены/обновлены.
)
echo.
pause
goto menu


:: --- Блок сборки: ПОЛНОСТЬЮ ПОРТАТИВНАЯ ---
:build_full_portable
call :build_app_base "ПОЛНОСТЬЮ ПОРТАТИВНАЯ"
"%PYTHON_EXE%" -m PyInstaller ^
main.py ^
--windowed ^
--name="%AppName%" ^
--clean ^
--icon="gemini_translator\GT.ico" ^
--noconfirm ^
--collect-data="PyQt6" ^
--collect-data="emoji" ^
--collect-data="jieba" ^
--collect-data="lxml" ^
--collect-data="setuptools" ^
--collect-data="werkzeug" ^
--hidden-import="PyQt6.sip" ^
--hidden-import="gemini_translator.api.handlers.gemini" ^
--hidden-import="gemini_translator.api.handlers.browser" ^
--hidden-import="gemini_translator.api.handlers.dry_run" ^
--hidden-import="gemini_translator.api.handlers.huggingface" ^
--hidden-import="gemini_translator.api.handlers.local" ^
--hidden-import="gemini_translator.api.handlers.openrouter" ^
--onefile ^
--add-data "config;config" ^
--add-data "README.md;."
call :build_app_end
goto :eof


:: --- Блок сборки: ГИБРИДНАЯ ---
:build_hybrid
call :build_app_base "ГИБРИДНАЯ"
"%PYTHON_EXE%" -m PyInstaller ^
main.py ^
--windowed ^
--name="%AppName%" ^
--clean ^
--icon="gemini_translator\GT.ico" ^
--noconfirm ^
--collect-data="PyQt6" ^
--collect-data="emoji" ^
--collect-data="jieba" ^
--collect-data="lxml" ^
--collect-data="setuptools" ^
--collect-data="werkzeug" ^
--hidden-import="PyQt6.sip" ^
--hidden-import="gemini_translator.api.handlers.gemini" ^
--hidden-import="gemini_translator.api.handlers.browser" ^
--hidden-import="gemini_translator.api.handlers.dry_run" ^
--hidden-import="gemini_translator.api.handlers.huggingface" ^
--hidden-import="gemini_translator.api.handlers.local" ^
--hidden-import="gemini_translator.api.handlers.openrouter" ^
--onefile ^
--add-data "config;config" ^
--add-data "README.md;."
if %ERRORLEVEL% EQU 0 (
    echo.
    echo [+] Этап 3 из 3: Копирование внешних данных...
    xcopy "config" "dist\config\" /E /I /Y /Q > nul
    copy /Y "README.md" "dist\README.md" > nul
    echo [OK] Данные скопированы.
)
call :build_app_end
goto :eof


:: --- Блок сборки: ПРОДВИНУТАЯ ---
:build_advanced
call :build_app_base "ПРОДВИНУТАЯ"
"%PYTHON_EXE%" -m PyInstaller ^
main.py ^
--windowed ^
--name="%AppName%" ^
--clean ^
--icon="gemini_translator\GT.ico" ^
--noconfirm ^
--collect-data="PyQt6" ^
--collect-data="emoji" ^
--collect-data="jieba" ^
--collect-data="lxml" ^
--collect-data="setuptools" ^
--collect-data="werkzeug" ^
--hidden-import="PyQt6.sip" ^
--hidden-import="gemini_translator.api.handlers.gemini" ^
--hidden-import="gemini_translator.api.handlers.browser" ^
--hidden-import="gemini_translator.api.handlers.dry_run" ^
--hidden-import="gemini_translator.api.handlers.huggingface" ^
--hidden-import="gemini_translator.api.handlers.local" ^
--hidden-import="gemini_translator.api.handlers.openrouter" ^
--add-data "config;config" ^
--add-data "README.md;."
if %ERRORLEVEL% EQU 0 (
    echo.
    echo [+] Этап 3 из 3: Копирование внешних данных...
    xcopy "config" "dist\%AppName%\config\" /E /I /Y /Q > nul
    copy /Y "README.md" "dist\%AppName%\README.md" > nul
    echo [OK] Данные скопированы.
)
call :build_app_end
goto :eof


:: --- ОБЩАЯ ЛОГИКА СБОРКИ ---
:build_app_base
cls
echo --- Полный цикл сборки (%~1 версия) ---
echo.
echo [+] Этап 1 из 3: Установка/обновление всех зависимостей и инструментов...
"%PYTHON_EXE%" -m pip install --upgrade pip
"%PYTHON_EXE%" -m pip install --upgrade -r "requirements.txt" pyinstaller pyinstaller-hooks-contrib
if %ERRORLEVEL% NEQ 0 (
    echo [!!!] Ошибка при установке зависимостей. Проверьте подключение к интернету.
    pause
    goto menu
)
echo [+] Инструменты для сборки готовы.
echo.
echo [+] Этап 2 из 3: Запуск PyInstaller для сборки "%AppName%"...
echo.
goto :eof


:build_app_end
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!!!] СБОРКА ЗАВЕРШИЛАСЬ С ОШИБКОЙ!
    echo     Просмотрите сообщения выше, чтобы найти причину.
) else (
    echo.
    echo [OK] СБОРКА УСПЕШНО ЗАВЕРШЕНА!
    echo     Готовое приложение находится в папке 'dist'.
)
echo.
echo [+] Процесс завершен.
pause
goto :eof
