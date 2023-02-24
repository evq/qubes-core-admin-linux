# coding=utf-8
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2022  Piotr Bartman <prbartman@invisiblethingslab.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301,
# USA.

import os
import shutil
import signal
import tempfile
from os.path import join
from subprocess import Popen
from subprocess import CalledProcessError
from typing import List, Tuple

import qubesadmin
from vmupdate.agent.source.args import AgentArgs
from vmupdate.agent.source.log_congfig import LOGPATH, LOG_FILE
from vmupdate.agent.source.status import StatusInfo, FinalStatus


class QubeConnection:
    """
    Run scripts in the qube.

    1. Initialize the state of connection.
    2. Transfer files to a new directory, start the qube if not running.
    3. Run an entrypoint script, return the output.
    4. On close, remove the created directory,
       stop the qube if it was started by this connection.
    """

    def __init__(
            self,
            qube,
            dest_dir,
            cleanup,
            logger,
            show_progress,
            status_notifier
    ):
        self.qube = qube
        self.dest_dir = dest_dir
        self.cleanup = cleanup
        self.logger = logger
        self.show_progress = show_progress
        self.status_notifier = status_notifier
        self.status_notified = False
        self._initially_running = None
        self.__connected = False

    def __enter__(self):
        self._initially_running = self.qube.is_running()
        self.__connected = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Do cleanup.

        1. If a progress collector is provided, send a signal that the update
           has been completed.
        2. Delete the uploaded files from the updated qube.
        3. Shut down qube if it wasn't running before the update.
        """
        if not self.status_notified:
            self.status_notifier.put(
                StatusInfo.done(self.qube, FinalStatus.SUCCESS))

        if self.cleanup:
            self.logger.info('Remove %s', self.dest_dir)
            self._run_shell_command_in_qube(
                self.qube, ['rm', '-r', self.dest_dir])

        if self.qube.is_running() and not self._initially_running:
            self.logger.info('Shutdown %s', self.qube.name)
            self.qube.shutdown()

        self.__connected = False

    def transfer_agent(self, src_dir: str) -> Tuple[int, List[str]]:
        """
        Copy a directory content to the workdir in the qube.

        :param src_dir: str: path to local (dom0) directory
        """
        assert self.__connected  # open the connection first

        arch_format = ".tar.gz"

        arch_dir = tempfile.mkdtemp()
        root_dir = os.path.dirname(src_dir)
        base_dir = os.path.basename(src_dir.strip(os.sep))
        src_arch = join(arch_dir, base_dir + arch_format)
        dest_arch = join(self.dest_dir, base_dir + arch_format)
        shutil.make_archive(base_name=join(arch_dir, base_dir),
                            format='gztar', root_dir=root_dir,
                            base_dir=base_dir)

        command = ['mkdir', '-p', self.dest_dir]
        exit_code, output = self._run_shell_command_in_qube(
            self.qube, command)
        if exit_code:
            return exit_code, output

        exit_code_, output_ = self._copy_file_from_dom0(src_arch, dest_arch)
        exit_code = max(exit_code, exit_code_)
        output += output_
        if exit_code:
            return exit_code, output

        command = ["tar", "-xzf", dest_arch, "-C", self.dest_dir]
        exit_code_, output_ = self._run_shell_command_in_qube(
            self.qube, command)
        exit_code = max(exit_code, exit_code_)
        output += output_

        return exit_code, output

    def _copy_file_from_dom0(self, src, dest):
        qvm_run = ["qvm-run", "--user=root", "--pass-io", self.qube.name]
        write_dest = ["cat", ">", dest]
        command = [*qvm_run, " ".join(write_dest)]
        self.logger.debug("run command: %s < %s", " ".join(command), src)
        try:
            with open(src, 'rb') as file:
                proc = Popen(command, stdin=file)
                proc.communicate()
                ret_code = proc.returncode
            if ret_code:
                raise OSError(f"Command returns code: {ret_code}")
            output = ""
        except OSError as exc:
            ret_code = 1
            output = str(exc)

        return ret_code, output

    def run_entrypoint(
            self, entrypoint_path: str, agent_args
    ) -> Tuple[int, List[str]]:
        """
        Run a script in the qube.

        :param entrypoint_path: path to the entrypoint.py in the qube
        :param agent_args: args for agent entrypoint
        :return: return code and output of the script
        """
        # make sure entrypoint is executable
        command = ['chmod', 'u+x', entrypoint_path]
        exit_code, output = self._run_shell_command_in_qube(self.qube, command)

        # run entrypoint
        command = [entrypoint_path, *AgentArgs.to_cli_args(agent_args)]
        exit_code_, output_ = self._run_shell_command_in_qube(
            self.qube, command, show=self.show_progress)
        exit_code = max(exit_code, exit_code_)
        output += output_

        return exit_code, output

    def read_logs(self) -> Tuple[int, List[str]]:
        """
        Read vm logs file.
        """
        command = ['cat',
                   str(join(LOGPATH, LOG_FILE))]
        exit_code, output = self._run_shell_command_in_qube(self.qube, command)
        return exit_code, output

    def _run_shell_command_in_qube(
            self, target, command: List[str], show: bool = False
    ) -> Tuple[int, List[str]]:
        self.logger.debug("run command in %s: %s",
                          target.name, " ".join(command))
        if not show:
            ret_code, untrusted_stdout_and_stderr = \
                self._run_command_and_wait_for_output(target, command)
        else:
            ret_code, untrusted_stdout_and_stderr = \
                self._run_command_and_actively_report_progress(
                    target, command)

        return ret_code, QubeConnection._collect_output(
            *untrusted_stdout_and_stderr)

    def _run_command_and_wait_for_output(
            self, target, command: List[str]
    ) -> Tuple[int, List[str]]:
        try:
            untrusted_stdout_and_stderr = target.run_with_args(
                *command, user='root'
            )
            ret_code = 0
        except CalledProcessError as err:
            if err.returncode == 100:
                self.status_notifier.put(
                    StatusInfo.done(self.qube, FinalStatus.NO_UPDATES))
                self.status_notified = True
                ret_code = 0
            else:
                self.logger.error(str(err))
                ret_code = err.returncode
            untrusted_stdout_and_stderr = (err.output, err.output)
        return ret_code, untrusted_stdout_and_stderr

    def _run_command_and_actively_report_progress(
            self, target, command: List[str]
    ) -> Tuple[int, Tuple[bytes, bytes]]:
        proc = target.run_service(
            'qubes.VMExec+' + qubesadmin.utils.encode_for_vmexec(command),
            user='root',
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)
        )

        stderr = self._collect_stderr(proc)
        stdout = self._collect_stdout(proc)

        proc.wait()
        untrusted_stdout_and_stderr = (stdout, stderr)
        ret_code = proc.returncode
        if ret_code == 100:
            self.status_notifier.put(
                StatusInfo.done(self.qube, FinalStatus.NO_UPDATES))
            self.status_notified = True
            ret_code = 0

        return ret_code, untrusted_stdout_and_stderr

    def _collect_stderr(self, proc) -> bytes:
        stderr = b""
        progress_finished = False
        for untrusted_line in iter(proc.stderr.readline, ''):
            if untrusted_line:
                if not progress_finished:
                    line = QubeConnection._string_sanitization(
                        untrusted_line.decode().rstrip())
                    try:
                        progress = float(line)
                    except ValueError:
                        stderr += untrusted_line
                        continue

                    if progress == 100.:
                        progress_finished = True
                    self.status_notifier.put(
                        StatusInfo.pending(self.qube, progress))
                else:
                    stderr += untrusted_line
            else:
                break
        proc.stderr.close()

        return stderr

    @staticmethod
    def _collect_stdout(proc) -> bytes:
        stdout = b""

        for untrusted_line in iter(proc.stdout.readline, ''):
            if untrusted_line:
                stdout += untrusted_line
            else:
                break
        proc.stdout.close()

        return stdout

    @staticmethod
    def _collect_output(
            untrusted_stdout: bytes, untrusted_stderr: bytes
    ) -> List[str]:
        untrusted_stdout = untrusted_stdout.decode('ascii', errors='ignore') + \
                           untrusted_stderr.decode('ascii', errors='ignore')

        # removing control characters
        stdout_lines = [QubeConnection._string_sanitization(line)
                        for line in untrusted_stdout.splitlines()]
        return stdout_lines

    @staticmethod
    def _string_sanitization(line: str) -> str:
        """
        Removing control characters
        """
        return ''.join([c for c in line if 0x20 <= ord(c) <= 0x7e])
