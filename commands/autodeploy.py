import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime
import json
import os
from string import Template

import paramiko
from django.conf import settings

from api.consumers import emit_notification
from apigateway.models import APIGateWay, Upstreams
from commands.apigateway import apigateway_event
from main.models import Middlewares, TaskLog, Business
from utils import git, svn
from utils.common import utc2local


class AutoDeploy(object):
    def __init__(self, **kwargs):
        self.task_id = str(kwargs['id'])
        self.instance = kwargs['instance']
        self.module = kwargs['module']
        self.layout = Middlewares.objects.get(name=kwargs['layout'])
        self.serial = kwargs['serial']
        self.updownline = kwargs['updownline']
        self.task_log = TaskLog.objects.get(id=self.instance.log_id) if len(self.instance.log_id) > 0 else TaskLog()
        self.servers = json.loads(self.instance.servers)
        self.logfile = settings.SALT_LOG + '/bs/%s.log' % self.task_id
        self.logtext = []
        self.params = None
        self.command = None
        self.text = None

        if not os.path.exists(settings.SALT_LOG + '/bs'):
            os.makedirs(settings.SALT_LOG + '/bs', 0o755)
        if not os.path.exists(os.path.join(settings.SALT_DEPLOY_TEMP, self.instance.modules)):
            os.makedirs(os.path.join(settings.SALT_DEPLOY_TEMP, self.instance.modules), 0o755)
        if os.path.exists(self.logfile):
            with open(self.logfile, 'r+') as f:
                f.truncate()
        if self.layout.layout_arch == 'bash':
            content = json.loads(self.layout.content)
            self.deploy_delay = int(content['deploy_delay'])

        if self.updownline:
            self.gateway = APIGateWay.objects.get(id__in=json.loads(kwargs['gateway']))

        self.destory(status='running')

    @staticmethod
    def build_time():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _generate_workspace(self):
        workspace = os.path.join(settings.SALT_DEPLOY_TEMP, '_'.join([self.instance.project, self.instance.modules]))
        if not os.path.exists(workspace):
            os.makedirs(workspace, 0o755)
        static_dir = os.path.join(settings.SALT_STATIC, 'deploy')
        if not os.path.exists(static_dir):
            os.makedirs(static_dir, 0o755)
        return workspace

    def _generate_version(self):
        local_st = utc2local(self.instance.created_at)
        return local_st.strftime('%Y%m%d%H%M%S')

    def _getfile(self):
        if self.instance.file_mode == '1':
            return '.'
        else:
            files = self.instance.file_list.split('\n')
            return ' '.join(files)

    @staticmethod
    def _excludes(excludes):
        excludes_cmd = ''

        # 无论是否填写排除.git和.svn, 这两个目录都不会发布
        excludes.append('.git')
        excludes.append('.svn')

        # 去重复
        excludes = list(set(excludes))

        for exclude in excludes:
            if exclude != '':
                excludes_cmd += "--exclude=%s " % exclude
        return excludes_cmd.strip()

    def _emit_notification(self, message):
        emit_notification(self.task_id, message)

    def _mq_notification(self, message):
        pass

    def local_command(self, cmd):
        (recode, data) = subprocess.getstatusoutput(cmd)
        self.text = str(data)
        if recode == 0:
            return True
        else:
            print(data)
            return False

    def ssh_command(self, cmd, logfile):
        ENV = ['export LANG=zh_CN.UTF-8', 'export LC_CTYPE=zh_CN.UTF-8']
        try:
            ssh_client = paramiko.SSHClient()
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            if settings.SALT_MASTER_PASS:
                ssh_client.connect(settings.SALT_MASTER, username=settings.SALT_MASTER_USER,
                                   password=settings.SALT_MASTER_PASS, port=settings.SALT_MASTER_PORT, timeout=6000)
            elif settings.SALT_MASTER_KEY:
                ssh_key = paramiko.RSAKey.from_private_key_file(settings.SALT_MASTER_KEY)
                ssh_client.connect(settings.SALT_MASTER, username=settings.SALT_MASTER_USER, pkey=ssh_key,
                                   port=settings.SALT_MASTER_PORT, timeout=6000)
            else:
                ssh_client.connect(settings.SALT_MASTER, username=settings.SALT_MASTER_USER,
                                   port=settings.SALT_MASTER_PORT, timeout=6000)
        except (socket.error, paramiko.AuthenticationException, paramiko.SSHException) as message:
            print("ERROR: SSH connection to " + settings.SALT_MASTER + " failed: " + str(message))
            sys.exit(1)
        command = " && ".join(ENV)
        command = command + " && " + cmd
        stdin, stdout, stderr = ssh_client.exec_command(command)
        data = ''.join(stdout.readlines())
        self.text = data
        with open(logfile, 'a') as f:
            f.write(data)
        code = stdout.channel.recv_exit_status()
        if code == 0:
            return True
        else:
            return False

    @staticmethod
    def set_color(line):
        line = line.replace('[0;0m', '')
        line = line.replace('', '')
        line = line.replace(' ', '&nbsp;')
        if ('Summary' in line) or ('------------' in line) or ('Total' in line):
            line = line.replace('[0;36m', '')
            line = {'color': 'darkcyan', 'text': line}
        elif "[0;33m" in line:
            line = line.replace('[0;33m', '')
            line = {'color': 'orange', 'text': line}
        elif "[0;32m" in line:
            line = line.replace('[0;32m', '')
            line = {'color': 'green', 'text': line}
        elif ("[0;31m" in line) or ("[0;1;31m" in line):
            line = line.replace('[0;31m', '').replace('[0;1;31m', '')
            line = {'color': 'red', 'text': line}
        elif "[0;36m" in line:
            line = line.replace('[0;36m', '')
            line = {'color': 'darkcyan', 'text': line}
        elif "[1;35m" in line:
            line = line.replace('[1;35m', '')
            line = {'color': 'tomato', 'text': line}
        elif "[0;35m" in line:
            line = line.replace('[0;35m', '')
            line = {'color': 'purple', 'text': line}
        elif "[1;30m" in line:
            line = line.replace('[1;30m', '')
            line = {'color': 'black', 'text': line}
        elif "[0;34m" in line:
            line = line.replace('[0;34m', '')
            line = {'color': 'blue', 'text': line}
        elif "[0;1;33m" in line:
            line = line.replace('[0;1;33m', '')
            line = {'color': 'gold', 'text': line}
        else:
            line = {'color': 'red', 'text': line}
        return line

    @staticmethod
    def sftp_command(local_file, remote_dir, filename):
        ssh_client = paramiko.Transport((settings.SALT_MASTER, settings.SALT_MASTER_PORT))
        if settings.SALT_MASTER_PASS:
            ssh_client.connect(username='root', password=settings.SALT_MASTER_PASS)
        elif settings.SALT_MASTER_KEY:
            sftp_key = paramiko.RSAKey.from_private_key_file(settings.SALT_MASTER_KEY)
            ssh_client.connect(username='root', pkey=sftp_key)
        else:
            ssh_client.connect(username='root')
        sftp_client = paramiko.SFTPClient.from_transport(ssh_client)
        msg = ''
        try:
            sftp_client.stat(remote_dir)
        except IOError as e:
            msg = str(e)
        try:
            if 'No such file' in msg:
                sftp_client.mkdir(remote_dir)
            sftp_client.put(local_file, remote_dir + '/' + filename)
        except Exception as e:
            print(str(e))
        sftp_client.close()
        ssh_client.close()

    def build_repo(self):
        if self.module.repo_type == 'git':
            status = git.updateRepo(branch=self.instance.version, instance=self.module)
        else:
            status = svn.updateRepo(branch=self.instance.version, instance=self.module)

        if status:
            self.module.status = status
            self.module.save()
            message = {'color': 'darkcyan', 'text': '%s %s 切换到 %s 版本成功' % (self.build_time(), self.module.repo_type, self.instance.version)}
            self._emit_notification({'message': message})
            self.logtext.append(message)
            self.task_log.log_text = json.dumps(self.logtext)
            self.task_log.save()
            return True
        else:
            message = {'color': 'darkcyan', 'text': '%s %s 切换到 %s 版本失败' % (self.build_time(), self.module.repo_type, self.instance.version)}
            self._emit_notification({'message': message})
            self.logtext.append(message)
            self._emit_notification({'message': 'end'})
            self.task_log.log_text = json.dumps(self.logtext)
            self.task_log.save()
            self.instance.status = 'failed'
            self.instance.save()
            return False

    def build_params(self):
        params = dict()
        params['workspace'] = self._generate_workspace()
        params['version'] = self._generate_version()
        params['package_name'] = self.instance.version + '.tar.gz'
        params['package'] = 'deploy/' + params['package_name']
        params['temp_workspace'] = os.path.join(params['workspace'], params['version'])
        params['deploy_project'] = self.module.dest_repo.rstrip('/') + '/' + self.instance.project + '_' + self.module.name + '/' + params['version'] + '-' + self.instance.version
        params['deploy_root'] = self.module.dest_root
        params['module_name'] = self.instance.project + '_' + self.instance.modules
        self.params = params
        return True

    def build_workspace(self):
        """初始化宿主机临时空间"""
        git_dir, svn_dir, cmd = None, None, None
        if os.path.exists(self.params['temp_workspace']):
            recode = self.local_command("rm -rf %s" % self.params['temp_workspace'])
            if recode:
                print("清理临时目录(%s)成功" % self.params['temp_workspace'])
            else:
                print("清理临时目录(%s)失败" % self.params['temp_workspace'])
        if self.module.repo_type == 'git':
            git_dir = os.path.join(self.module.repo_work, self.module.env, self.module.name)
            cmd = ['cp -rf %s %s' % (git_dir, self.params['temp_workspace'])]
        if self.module.repo_type == 'svn':
            svn_dir = os.path.join(self.module.repo_work, self.module.env, self.module.name)
            cmd = ['cp -rf %s %s' % (svn_dir, self.params['temp_workspace'])]
        command = ' && '.join(cmd)
        print("执行命令： %s" % command)
        recode = self.local_command(command)
        if recode:
            text = "%s %s 代码拷贝到 %s 临时空间成功" % (self.build_time(), self.module.name, self.params['workspace'])
            print(text)
            message = {'color': 'darkcyan', 'text': text}
            self._emit_notification({'message': message})
            self.logtext.append(message)
            return recode
        else:
            text = "%s %s 代码拷贝到 %s 临时空间失败" % (self.build_time(), self.module.name, self.params['workspace'])
            print(text)
            message = {'color': 'red', 'text': text}
            self._emit_notification({'message': message})
            self._emit_notification({'message': 'end'})
            self.logtext.append(message)
            return recode

    def build_package(self):
        """部署资源打包"""
        excludes = self.module.repo_ignore.split('\n')
        package_name = os.path.join(self.params['workspace'], self.params['package_name'])
        cmd = ["cd %s" % self.params['temp_workspace'], "tar -p %s -cz -f %s %s" % (self._excludes(excludes), package_name, self._getfile())]
        command = ' && '.join(cmd)
        print("执行命令： %s" % command)
        recode = self.local_command(command)
        if recode:
            text = "%s %s 打包到 %s 成功" % (self.build_time(), self.params['package_name'], self.params['workspace'])
            print(text)
            message = {'color': 'darkcyan', 'text': text}
            self._emit_notification({'message': message})
            self.logtext.append(message)
            return recode
        else:
            text = "%s %s 打包到 %s 失败" % (self.build_time(), self.params['package_name'], self.params['workspace'])
            print(text)
            message = {'color': 'red', 'text': text}
            self._emit_notification({'message': message})
            self.logtext.append(message)
            return recode

    def build_copypackage(self):
        """拷贝部署包到静态资源目录"""
        static_dir = os.path.join(settings.SALT_STATIC, 'deploy')
        package_file = static_dir + '/' + self.params['package_name']
        if os.path.exists(package_file):
            os.remove(package_file)
        cmd = ['cp -rf %s %s' % (os.path.join(self.params['workspace'], self.params['package_name']), static_dir)]
        command = ' && '.join(cmd)
        print("执行命令： %s" % command)
        recode = self.local_command(command)
        if recode:
            text = "%s 拷贝(%s)部署包到(%s)静态资源目录成功" % (self.build_time(), self.params['package_name'], 'static')
            message = {'color': 'darkcyan', 'text': text}
            self._emit_notification({'message': message})
            self.logtext.append(message)
            return recode
        else:
            text = "%s 拷贝(%s)部署包到(%s)静态资源目录失败" % (self.build_time(), self.params['package_name'], 'static')
            message = {'color': 'red', 'text': text}
            self._emit_notification({'message': message})
            self._emit_notification({'message': 'end'})
            self.logtext.append(message)
            return recode

    def build_saltstack(self):
        """生成saltstack state.sls 需要的文件"""
        state_file = os.path.join(settings.SALT_DEPLOY_TEMP, self.instance.modules, self.layout.name + '-' + self.layout.version.replace('.', '') + '.sls')
        if os.path.exists(state_file):
            os.remove(state_file)
        with open(state_file, 'w') as f:
            f.write(self.layout.content)
        self.params['state_file'] = os.path.join(self.instance.modules, self.layout.name + '-' + self.layout.version.replace('.', ''))
        filename = self.layout.name + '-' + self.layout.version.replace('.', '') + '.sls'
        self.sftp_command(local_file=state_file, remote_dir='/srv/salt/' + self.module.name, filename=filename)
        message = {'color': 'darkcyan', 'text': '%s 生成%s文件成功' % (self.build_time(), filename)}
        self.logtext.append(message)
        self._emit_notification({'message': message})
        return True

    def build_bash(self, args=None):
        """生成bash编排内容"""
        command = None
        if self.layout.layout_arch == 'bash':
            content = json.loads(self.layout.content)
            command = content[args].split('\n')
        elif args is None:
            command = self.layout.content.split('\n')
        if len(command) > 2:
            return ' && '.join(command)
        else:
            return ''.join(command)

    def build_macro(self, command):
        sub = dict()
        if '${workspace}' in command:
            sub['workspace'] = self.params['workspace']
        if '${package}' in command:
            sub['package'] = self.params['package']
        if '${package_name}' in command:
            sub['package_name'] = self.params['package_name']
        if '${host}' in command:
            sub['host'] = self.params['host']
        if '${deploy_project}' in command:
            sub['deploy_project'] = self.params['deploy_project']
        if '${deploy_root}' in command:
            sub['deploy_root'] = self.params['deploy_root']
        if '${module_name}' in command:
            sub['module_name'] = self.params['module_name']
        t = Template(command)
        command = t.substitute(sub)
        return command

    def build_cmd_file(self):
        filename = '%s.sh' % str(uuid.uuid1())
        if not os.path.exists(os.path.join(settings.SALT_STATIC, 'custom_cmd')):
            os.makedirs(os.path.join(settings.SALT_STATIC, 'custom_cmd'))
        with open(os.path.join(settings.SALT_STATIC, 'custom_cmd', filename), 'w') as f:
            f.write(self.command)
        return filename

    def build_cmd(self, servers):
        tgt = ', '.join(servers)
        pillar = json.dumps(self.params)
        if self.layout.layout_arch == 'bash' or self.layout.layout_arch == 'bash_simple':
            if len(servers) <= 1:
                cmd = "salt '%s' cmd.run '%s' pillar='%s' --force-color" % (tgt, self.command, '{}')
            else:
                cmd = "salt -L '%s' cmd.run '%s' pillar='%s' --force-color" % (tgt, self.command, '{}')
            return cmd
        else:
            if len(servers) <= 1:
                cmd = "salt '%s' state.sls %s pillar='%s' --force-color" % (tgt, self.params['state_file'], pillar)
            else:
                cmd = "salt -L '%s' state.sls %s pillar='%s' --force-color" % (tgt, self.params['state_file'], pillar)
            return cmd

    def build_nginx_gateway(self, server, online='up'):
        if not self.gateway:
            message = {'color': 'red', 'text': 'Nginx网关没有找到'}
            self.logtext.append(message)
            self._emit_notification({'message': message})
            return False
        if not Upstreams.objects.filter(apigateway_id=self.gateway.id).exists():
            message = {'color': 'red', 'text': '%s Nginx网关没有找到upstreams数据' % self.build_time() }
            self.logtext.append(message)
            self._emit_notification({'message': message})
            return False
        upstreams = Upstreams.objects.filter(apigateway_id=self.gateway.id)
        for upstream in upstreams:
            nodedata = json.loads(upstream.upstreams)
            if not nodedata:
                message = {'color': 'red', 'text': '%s Nginx网关的upstreams没有节点数据' % self.build_time()}
                self.logtext.append(message)
                self._emit_notification({'message': message})
                return False
            for node in nodedata:
                if node['address'] == server and online == 'up':
                    node['status'] = True
                    recode, self.text = apigateway_event(task_id=self.gateway.id, deploy_type='upstreams', category_id=upstream.id)
                    if not recode:
                        message = {'color': 'red', 'text': '%s Nginx网关的upstreams的%s节点上线失败' % (self.build_time(), server)}
                        self.logtext.append(message)
                        self._emit_notification({'message': message})
                        return False
                    else:
                        message = {'color': 'green', 'text': '%s Nginx网关的upstreams的%s节点上线成功' % (self.build_time(), server)}
                        self.logtext.append(message)
                        self._emit_notification({'message': message})
                        upstream.upstreams = json.dumps(nodedata)
                        upstream.save()
                        return True
                if node['address'] == server and online == 'down':
                    node['status'] = False
                    recode, self.text = apigateway_event(task_id=str(self.gateway.id), deploy_type='upstreams', category_id=str(upstream.id))
                    if not recode:
                        message = {'color': 'red', 'text': '%s Nginx网关的upstreams的%s节点下线失败' % (self.build_time(), server)}
                        self.logtext.append(message)
                        self._emit_notification({'message': message})
                        return False
                    else:
                        message = {'color': 'green', 'text': '%s Nginx网关的upstreams的%s节点下线成功' % (self.build_time(), server)}
                        self.logtext.append(message)
                        self._emit_notification({'message': message})
                        upstream.upstreams = json.dumps(nodedata)
                        upstream.save()
                        return True
        message = {'color': 'red', 'text': '%s Nginx网关的upstreams的%s节点没有找到' % (self.build_time(), server)}
        self.logtext.append(message)
        self._emit_notification({'message': message})
        return False

    def deploy_pre(self):
        """自定义命令，发部之前调用"""
        command = self.build_bash(args='deploy_pre')
        self.command = self.build_macro(command)
        print("Bash部署前命令: %s" % self.command)
        if not self.local_command(self.command):
            self.destory(status='failed')
            self._emit_notification({'message': 'end'})
            return False
        return True

    def deploy_release(self, server):
        """自定义命令，发部调用"""
        command = self.build_bash(args='deploy_release')
        self.command = self.build_macro(command)
        cmd = self.build_cmd([server])
        print("Bash部署命令: %s" % cmd)
        if not self.ssh_command(cmd, self.logfile):
            self.destory(status='failed')
            self._emit_notification({'message': 'end'})
            return False
        return True

    def deploy_post(self, server):
        """自定义命令，发部之后调用"""
        command = self.build_bash(args='deploy_post')
        self.command = self.build_macro(command)
        cmd = self.build_cmd([server])
        print("Bash部署后命令: %s" % cmd)
        if not self.ssh_command(cmd, self.logfile):
            self.destory(status='failed')
            self._emit_notification({'message': 'end'})
            return False
        return True

    def pre_run(self):
        """发部之前的准备环境"""
        if not self.build_params():
            self.destory(status='failed')
            self._emit_notification({'message': 'end'})
            return False
        if not self.build_repo():
            self.destory(status='failed')
            self._emit_notification({'message': 'end'})
            return False
        if not self.build_workspace():
            self.destory(status='failed')
            self._emit_notification({'message': 'end'})
            return False
        if not self.build_package():
            self.destory(status='failed')
            self._emit_notification({'message': 'end'})
            return False
        if not self.build_copypackage():
            self.destory(status='failed')
            self._emit_notification({'message': 'end'})
            return False
        return True

    def bash_run(self):
        """自定义命令编排发部入口"""
        status = False
        for server in self.servers:
            self._emit_notification({'message': {'color': 'green', 'text': '%s %s 开始部署前任务' % (self.build_time(), server)}})
            if not self.deploy_pre():
                self.destory(status='failed')
                self._emit_notification({'message': 'end'})
                return status
            self._emit_notification({'message': {'color': 'green', 'text': '%s %s 开始部署任务' % (self.build_time(), server)}})
            if not self.deploy_release(server):
                self.destory(status='failed')
                self._emit_notification({'message': 'end'})
                return status
            self._emit_notification({'message': {'color': 'green', 'text': '%s %s 开始部署后任务' % (self.build_time(), server)}})
            if not self.deploy_post(server):
                self.destory(status='failed')
                self._emit_notification({'message': 'end'})
                return status
            if self.deploy_delay != 0:
                self._emit_notification({'message': {'color': 'green', 'text': '%s %s 开始部署等待%s秒' % (self.build_time(), server, self.deploy_delay)}})
                time.sleep(self.deploy_delay)
            self._emit_notification({'message': {'color': 'green', 'text': '%s %s 部署完成' % (self.build_time(), server)}})
        self.destory(status='success')
        status = True
        self._emit_notification({'message': {'color': 'green', 'text': '%s 部署完成' % self.build_time()}})
        self._emit_notification({'message': 'end'})
        return status

    def bash_simple_run(self):
        """自定义命令发部入口"""
        for server in self.servers:
            self.params['host'] = server
            if self.updownline:
                ret = self.build_nginx_gateway(server, online='down')
                if not ret:
                    self.destory(status='failed')
                    self._emit_notification({'message': 'end'})
                    return False
            self._emit_notification({'message': {'color': 'green', 'text': '%s %s 开始业务部署' % (self.build_time(), server)}})
            command = self.build_bash()
            self.command = self.build_macro(command)
            cmd = self.build_cmd([server])
            print("执行命令： %s" % cmd)
            if not self.ssh_command(cmd, self.logfile):
                self.destory(status='failed')
                self._emit_notification({'message': 'end'})
                return False, 'failed'
            self._emit_notification({'message': {'color': 'green', 'text': '%s %s 开始业务部署完成' % (self.build_time(), server)}})
            if self.updownline:
                if not self.build_nginx_gateway(server, online='up'):
                    self.destory(status='failed')
                    self._emit_notification({'message': 'end'})
                    return False, 'failed'
        self.destory(status='success')
        self._emit_notification({'message': 'end'})
        return True, 'success'

    def salt_serial_run(self):
        """salt state 发部串行入口"""
        status = False
        if not self.build_saltstack():
            self.destory(status='failed')
            self._emit_notification({'message': 'end'})
            return False
        for server in self.servers:
            self.params['host'] = server
            if self.updownline:
                ret = self.build_nginx_gateway(server, online='down')
                if not ret:
                    self.destory(status='failed')
                    self._emit_notification({'message': 'end'})
                    return False, 'failed'
            cmd = self.build_cmd([server])
            print("执行命令： %s" % cmd)
            self._emit_notification({'message': {'color': 'green', 'text': '%s %s 开始业务部署' % (self.build_time(), server)}})
            if not self.ssh_command(cmd, self.logfile):
                self.destory(status='failed')
                self._emit_notification({'message': 'end'})
                return status, 'failed'
            else:
                self._emit_notification({'message': {'color': 'green', 'text': '%s %s 开始业务部署完成' % (self.build_time(), server)}})
            if self.updownline:
                if not self.build_nginx_gateway(server, online='up'):
                    self.destory(status='failed')
                    self._emit_notification({'message': 'end'})
                    return False, 'failed'
        status = True
        self.destory(status='success')
        self._emit_notification({'message': 'end'})
        return status, 'success'

    def salt_run(self):
        """salt state 发部并行入口"""
        if not self.build_saltstack():
            self.destory(status='failed')
            self._emit_notification({'message': 'end'})
            return False
        cmd = self.build_cmd(self.servers)
        print("执行命令： %s" % cmd)
        recode = self.ssh_command(cmd, self.logfile)
        if recode:
            self.destory(status='success')
            self._emit_notification({'message': 'end'})
            return recode, 'success'
        else:
            self.destory(status='failed')
            self._emit_notification({'message': 'end'})
            return recode, 'failed'

    def deploy_run(self):
        """发部入口"""
        if not self.pre_run():
            print("环境初始化失败")
            return False
        if self.updownline:
            if self.layout.layout_arch == 'bash_simple':
                return self.bash_simple_run()
            elif self.layout.layout_arch == 'bash':
                self.destory(status='failed')
                self._emit_notification({'message': {'color': 'red', 'text': '%s Bash编排自带上下线,无法在启用上下线' % self.build_time()}})
                self._emit_notification({'message': 'end'})
            else:
                return self.salt_serial_run()
        elif self.serial:
            if self.layout.layout_arch == 'bash':
                return self.bash_run()
            elif self.layout.layout_arch == 'bash_simple':
                return self.bash_simple_run()
            else:
                return self.salt_serial_run()
        else:
            if self.layout.layout_arch == 'bash':
                return self.bash_run()
            elif self.layout.layout_arch == 'bash_simple':
                return self.bash_simple_run()
            else:
                return self.salt_run()

    def destory(self, status):
        if status == 'success':
            Business.objects.filter(project=self.module.project, modules=self.module.name).exclude(id=self.instance.id).update(current=False)
            self.instance.current = True
        self.instance.status = status
        self.instance.log_id = self.task_log.id
        self.instance.save()
        self.task_log.log_text = json.dumps(self.logtext)
        self.task_log.save()
