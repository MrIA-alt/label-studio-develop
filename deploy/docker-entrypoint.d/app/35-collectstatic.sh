#!/bin/bash
echo >&3 "=> Collecting static files..."
python3 /label-studio/label_studio/manage.py collectstatic --noinput >&3 2>&3
echo >&3 "=> Static files collected"
