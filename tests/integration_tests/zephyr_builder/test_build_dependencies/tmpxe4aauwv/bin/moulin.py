#!/bin/sh
/usr/bin/python3 /home/vladyslav_goncharuk/Projects/new_dev/moulin-svlad-90/moulin.py "$@"
status=$?
echo "$2 $3 $status" >> "/home/vladyslav_goncharuk/Projects/new_dev/moulin-svlad-90/tests/integration_tests/zephyr_builder/test_build_dependencies/tmpxe4aauwv/dep-invocations.log"
if [ "$2" = "--dep" ] && [ -f ".moulin_$3.d" ]; then
    cp ".moulin_$3.d" "/home/vladyslav_goncharuk/Projects/new_dev/moulin-svlad-90/tests/integration_tests/zephyr_builder/test_build_dependencies/tmpxe4aauwv/depfile.d"
fi
exit $status
