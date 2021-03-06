import commands
import os
import time
import re
import tempfile
from urlparse import urlparse
from datetime import datetime
import string
from collections import namedtuple
import pexpect
from nose.tools import assert_equals, assert_true, assert_false
from pprint import pformat
from lettuce_bunch.special import get_current_bunch_dir

# Make Bash an object
import conf


class command_output(object):
    def __init__(self, output):
        self.output = output

    def successful(self):
        return self.output[0] == 0

    def output_contains_pattern(self, pattern):
        regex2match = re.compile(pattern)
        search_result = regex2match.search(self.output[1])
        return (not search_result is None) and len(search_result.group()) > 0

    def output_text(self):
        return self.output[1]

    def output_nonempty(self):
        return len(self.output) > 1 and len(self.output[1]) > 0

class bash(command_output):
    def __init__(self, cmdline):
        output = self.__execute(cmdline)
        super(bash,self).__init__(output)

    def __execute(self, cmd):
        retcode = commands.getstatusoutput(cmd)
        status, text = retcode
        conf.bash_log(cmd, status, text)
        return retcode



class rpm(object):

    @staticmethod
    def clean_all_cached_data():
        out = bash("sudo yum -q clean all")
        return out.successful()

    @staticmethod
    def install(package):
        out = bash("sudo yum -y install '%s'" % package)
        return out.successful() and out.output_contains_pattern("(Installed:[\s\S]*%s.*)|(Package.*%s.* already installed)" % (package, package))
        
    @staticmethod
    def remove(package):
        out = bash("sudo yum -y erase '%s'" % package)
        wildcards_stripped_pkg_name = package.strip('*')
        return out.output_contains_pattern("(No Match for argument)|(Removed:[\s\S]*%s.*)|(Package.*%s.*not installed)" % (wildcards_stripped_pkg_name , wildcards_stripped_pkg_name))

    @staticmethod
    def installed(package):
        out = bash("rpmquery %s" % package)
        return not out.output_contains_pattern('not installed')

    @staticmethod
    def available(package):
        out = bash("sudo yum list | grep '^%s\.'" % package)
        return out.successful() and out.output_nonempty()

    @staticmethod
    def yum_repo_exists(id):
        out = bash("sudo yum repolist | grep -E '^%s'" % id)
        return out.successful() and out.output_contains_pattern("%s" % id)


class EnvironmentRepoWriter(object):
    def __init__(self, repo, env_name=None):

        if env_name is None or env_name == 'master':
            repo_config = """
[{repo_id}]
name=Grid Dynamics OpenStack RHEL
baseurl=http://osc-build.vm.griddynamics.net/{repo_id}
enabled=1
gpgcheck=1

""".format(repo_id=repo)
        else:
            repo_config = """
[{repo_id}]
name=Grid Dynamics OpenStack RHEL
baseurl=http://osc-build.vm.griddynamics.net/unstable/{env}/{repo_id}
enabled=1
gpgcheck=1

""".format(repo_id=repo, env=env_name)
            pass

        self.__config = repo_config


    def write(self, file):
        file.write(self.__config)


class EscalatePermissions(object):

    @staticmethod
    def read(filename, reader):
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            tmp_file_path = tmp_file.name
        out = bash("sudo dd if=%s of=%s" % (filename, tmp_file_path))

        with open(tmp_file_path, 'r') as tmp_file:
            reader.read(tmp_file)
        bash("rm -f %s" % tmp_file_path)
        return out.successful()

    @staticmethod
    def overwrite(filename, writer):
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            writer.write(tmp_file)
            tmp_file_path = tmp_file.name
        out = bash("sudo dd if=%s of=%s" % (tmp_file_path, filename))
        bash("rm -f %s" % tmp_file_path)
        return out.successful() and os.path.exists(filename)


class mysql_cli(object):
    @staticmethod
    def create_db(db_name, admin_name="root", admin_pwd="root"):
        bash("mysqladmin -u%s -p%s -f drop %s" % (admin_name, admin_pwd, db_name))
        out = bash("mysqladmin -u%s -p%s create %s" % (admin_name, admin_pwd, db_name))
        return out.successful()

    @staticmethod
    def execute(sql_command, admin_name="root", admin_pwd="root"):
        out = bash('echo "%s" | mysql -u%s -p%s mysql' % (sql_command, admin_name, admin_pwd))
        return out

    @staticmethod
    def update_root_pwd( default_pwd="", admin_pwd="root"):
        out = bash('mysqladmin -u root password %s' %  admin_pwd)
        return out.successful()

    @staticmethod
    def grant_db_access_for_hosts(hostname,db_name, db_user, db_pwd, admin_name="root", admin_pwd="root"):
        sql_command =  "GRANT ALL PRIVILEGES ON %s.* TO '%s'@'%s' IDENTIFIED BY '%s';" % (db_name, db_user, hostname, db_pwd)
        return mysql_cli.execute(sql_command, admin_name, admin_pwd).successful()

    @staticmethod
    def grant_db_access_local(db_name, db_user, db_pwd, admin_name="root", admin_pwd="root"):
        sql_command =  "GRANT ALL PRIVILEGES ON %s.* TO %s IDENTIFIED BY '%s';" % (db_name, db_user, db_pwd)
        return mysql_cli.execute(sql_command, admin_name, admin_pwd).successful()

    @staticmethod
    def db_exists(db_name, admin_name="root", admin_pwd="root"):
        sql_command = "show databases;"
        out = mysql_cli.execute(sql_command, admin_name, admin_pwd)
        return out.successful() and out.output_contains_pattern("%s" % db_name)

    @staticmethod
    def user_has_all_privileges_on_db(username, db_name, admin_name="root", admin_pwd="root"):
        sql_command = "show grants for '%s'@'localhost';" %username
        out = mysql_cli.execute(sql_command, admin_name, admin_pwd)
        return out.successful() \
            and out.output_contains_pattern("GRANT ALL PRIVILEGES ON .*%s.* TO .*%s.*" % (db_name, username))

class service(object):
    def __init__(self, name):
        self.__name = name
        self.__unusual_running_patterns = {'rabbitmq-server': '(Node.*running)|(running_applications)'}
        self.__unusual_stopped_patterns = {'rabbitmq-server': 'no.nodes.running|no_nodes_running|nodedown'}
        self.__exec_by_expect = set(['rabbitmq-server'])

    def __exec_cmd(self, cmd):
        if self.__name in self.__exec_by_expect:
            return expect_run(cmd)

        return bash(cmd)

    def start(self):
        return self.__exec_cmd("sudo service %s start" % self.__name)

    def stop(self):
        return self.__exec_cmd("sudo service %s stop" % self.__name)

    def restart(self):
        return self.__exec_cmd("sudo service %s restart" % self.__name)

    def running(self):
        out = self.__exec_cmd("sudo service %s status" % self.__name)

        if self.__name in self.__unusual_running_patterns:
            return out.output_contains_pattern(self.__unusual_running_patterns[self.__name])

        return out.successful() \
            and out.output_contains_pattern("(?i)running") \
            and (not out.output_contains_pattern("(?i)stopped|unrecognized|dead|nodedown"))

    def stopped(self):
#        out = bash("sudo service %s status" % self.__name)
#        unusual_service_patterns = {'rabbitmq-server': 'no.nodes.running|no_nodes_running|nodedown'}
        out = self.__exec_cmd("sudo service %s status" % self.__name)

        if self.__name in self.__unusual_stopped_patterns:
            return out.output_contains_pattern(self.__unusual_stopped_patterns[self.__name])

        return (not out.output_contains_pattern("(?i)running")) \
            and out.output_contains_pattern("(?i)stopped|unrecognized|dead|nodedown")




class FlagFile(object):
    COMMENT_CHAR = '#'
    OPTION_CHAR =  '='

    def __init__(self, filename):
        self.__commented_options = set()
        self.options = {}
        self.__load(filename)

    def read(self, file):
        for line in file:
            comment = ''
            if FlagFile.COMMENT_CHAR in line:
                line, comment = line.split(FlagFile.COMMENT_CHAR, 1)
            if FlagFile.OPTION_CHAR in line:
                option, value = line.split(FlagFile.OPTION_CHAR, 1)
                option = option.strip()
                value = value.strip()
                if comment:
                    self.__commented_options.add(option)
                self.options[option] = value


    def __load(self, filename):
        EscalatePermissions.read(filename, self)

    def commented(self, option):
        return option in self.__commented_options

    def uncomment(self, option):
        if option in self.options and option in self.__commented_options:
            self.__commented_options.remove(option)

    def comment_out(self, option):
        if option in self.options:
            self.__commented_options.add(option)

    def write(self,file):
        for option, value in self.options.iteritems():
            comment_sign = FlagFile.COMMENT_CHAR if option in self.__commented_options else ''
            file.write("%s%s=%s\n" % (comment_sign, option, value))

    def apply_flags(self, pairs):
        for name, value in pairs:
            self.options[name.strip()] = value.strip()
        return self

    def verify(self, pairs):
        for name, value in pairs:
            name = name.strip()
            value = value.strip()
            if name not in self.options or self.options[name] != value:
                return False

        return True

    def overwrite(self, filename):
        return EscalatePermissions.overwrite(filename, self)

class novarc(dict):
    def __init__(self):
        super(novarc,self).__init__()

    def load(self, file):
        self.file = file
        return os.path.exists(file)

    def source(self):
        return "source %s" % self.file

class nova_cli(object):

    __novarc = None

    @staticmethod
    def novarc_available():
        return not (nova_cli.__novarc is None)

    @staticmethod
    def get_novarc_load_cmd():
        if nova_cli.novarc_available():
            return nova_cli.__novarc.source()

        return "/bin/false"

    @staticmethod
    def set_novarc(project, user, destination):
        if nova_cli.__novarc is None:
            new_novarc = novarc()
            path  = os.path.join(destination, 'novarc.zip')
            out = bash('sudo nova-manage project zipfile %s %s %s' % (project, user, path))
            if out.successful():
                out = bash("unzip -uo %s -d %s" % (path,destination))
                if out.successful() and new_novarc.load(os.path.join(destination, 'novarc')):
                    nova_cli.__novarc = new_novarc

        return nova_cli.__novarc

    @staticmethod
    def create_admin(username):
        out = bash("sudo nova-manage user admin %s" % username)
        return out.successful()

    @staticmethod
    def user_exists(username):
        out = bash("sudo nova-manage user list")
        return out.successful() and out.output_contains_pattern(".*%s.*" % username)

    @staticmethod
    def create_project(project_name, username):
        out = bash("sudo nova-manage project create %s %s" % (project_name, username))
        return out.successful()

    @staticmethod
    def project_exists(project):
        out = bash("sudo nova-manage project list")
        return out.successful() and out.output_contains_pattern(".*%s.*" % project)

    @staticmethod
    def user_is_project_admin(user, project):
        out = bash("sudo nova-manage project list --user=%s" % user)
        return out.successful() and out.output_contains_pattern(".*%s.*" % project)

    @staticmethod
    def create_network(cidr, nets, ips):
        out = bash('sudo nova-manage network create private "%s" %s %s' % (cidr, nets, ips))
        return out.successful()

    @staticmethod
    def network_exists(cidr):
        out = bash('sudo nova-manage network list')
        return out.successful() and out.output_contains_pattern(".*%s.*" % cidr)

    @staticmethod
    def vm_image_register(image_name, owner, disk, ram, kernel):
        out = bash('sudo nova-manage image all_register --image="%s" --kernel="%s" --ram="%s" --owner="%s" --name="%s"'
        % (disk, kernel, ram, owner, image_name))
        return out.successful()

    @staticmethod
    def vm_image_registered(name):
        return nova_cli.exec_novaclient_cmd('image-list | grep "%s"' % name)

    @staticmethod
    def add_keypair(name, public_key=None):
        public_key_param = "" if public_key is None else "--pub_key %s" % public_key
        return nova_cli.exec_novaclient_cmd('keypair-add %s %s' % (public_key_param, name))

    @staticmethod
    def keypair_exists(name):
        return nova_cli.exec_novaclient_cmd('keypair-list | grep %s' % name)

    @staticmethod
    def get_image_id_list(name):
        lines = nova_cli.get_novaclient_command_out("image-list | grep  %s | awk '{print $2}'" % name)
        id_list = lines.split(os.linesep)
        return id_list

    @staticmethod
    def start_vm_instance(name, image_id, flavor_id, key_name=None):
        key_name_arg = "" if key_name is None else "--key_name %s" % key_name
        return nova_cli.exec_novaclient_cmd("boot %s --image %s --flavor %s %s" % (name, image_id, flavor_id, key_name_arg))

    @staticmethod
    def terminate_instance(instance_id):
        return nova_cli.exec_novaclient_cmd("delete %s" % instance_id)

    @staticmethod
    def start_vm_instance_return_output(name, image_id, flavor_id, key_name=None):
        key_name_arg = "" if key_name is None else "--key_name %s" % key_name
        text =  nova_cli.get_novaclient_command_out("boot %s --image %s --flavor %s %s" % (name, image_id, flavor_id, key_name_arg))
        if text:
            return ascii_table(text)
        return None


    @staticmethod
    def get_flavor_id_list(name):
        lines = nova_cli.get_novaclient_command_out("flavor-list | grep  %s | awk '{print $2}'" % name)
        id_list = lines.split(os.linesep)
        return id_list


    @staticmethod
    def db_sync():
        out = bash("sudo nova-manage db sync")
        return out.successful()

    @staticmethod
    def exec_with_novarc_cmd(client, cmd):
        if nova_cli.novarc_available():
            source = nova_cli.get_novarc_load_cmd()
            out = bash('%s && %s %s' % (source, client, cmd))
            return out.successful()
        return False

    @staticmethod
    def exec_novaclient_cmd(cmd):
        return nova_cli.exec_with_novarc_cmd('nova', cmd)

    @staticmethod
    def exec_nova2ools_cmd(cmd):
        return nova_cli.exec_with_novarc_cmd('nova2ools-local-volumes', cmd)

    @staticmethod
    def get_novaclient_command_out(cmd):
        return nova_cli.get_with_novarc_command_out('nova', cmd)

    @staticmethod
    def get_local_volumes_command_out(cmd):
        return nova_cli.get_with_novarc_command_out('nova2ools-local-volumes', cmd)

    @staticmethod
    def get_with_novarc_command_out(client, cmd):
        if nova_cli.novarc_available():
            source = nova_cli.get_novarc_load_cmd()
            out = bash('%s && %s %s' % (source, client, cmd))
            garbage_list = ['DeprecationWarning', 'import md5', 'import sha']

            def does_not_contain_garbage(str_item):
                for item in garbage_list:
                    if item in str_item:
                        return False
                return True

            lines_without_warning = filter(does_not_contain_garbage, out.output_text().split(os.linesep))
            return string.join(lines_without_warning, os.linesep)
        return ""

    @staticmethod
    def get_instance_status(name):
        return nova_cli.get_novaclient_command_out("list | grep %s | sed 's/|.*|.*|\(.*\)|.*|/\\1/'" % name).strip()

    @staticmethod
    def get_instance_ip(name):
        command = "list | grep %s | sed -e 's/|.*|.*|.*|\(.*\)|/\\1/' | sed -r 's/(.*)((\\b[0-9]{1,3}\.){3}[0-9]{1,3}\\b)/\\2/'" % name
        return nova_cli.get_novaclient_command_out(command).strip()

    @staticmethod
    def get_instance_id(name):
        command = "list | grep %s | awk '{print $2}'" % name
        return nova_cli.get_novaclient_command_out(command).strip()

    @staticmethod
    def get_local_volume_id(device, vm_name):
        volumes = ascii_table(nova_cli.get_local_volumes_command_out('list --format "{id} {device} {instance_id}"'),
        ['Id', 'Device', 'Instance_Id'])
        instance_id = nova_cli.get_instance_id(vm_name)
        volume = volumes.select(['Id'],
            lambda x: x['Device'] == device and
                      x['Instance_Id'] == instance_id)

        if len(volume) == 1:
            return volume[0][0].strip()

        return None

    @staticmethod
    def get_local_volumes(format="{id} {instance_id} {device}", captions=("Id", "Instance_Id", "Device")):
        volumes = ascii_table(
                nova_cli.get_local_volumes_command_out(
                    'list --format "%s"' % format
                ),
                captions
            )
        return volumes

    @staticmethod
    def wait_instance_comes_up(name, timeout):
        return nova_cli.wait_instance_state(name, 'ACTIVE', timeout)

    @staticmethod
    def wait_instance_state(name, state, timeout):
        poll_interval = 5
        time_left = int(timeout)
        status = ""
        while time_left > 0:
            status =  nova_cli.get_instance_status(name).upper()
            if status != state:
                time.sleep(poll_interval)
                time_left -= poll_interval
            else:
                break
        return status == state


class misc(object):

    @staticmethod
    def kill_process(name):
        bash("sudo killall  %s" % name).successful()
        return True

    @staticmethod
    def unzip(zipfile, destination="."):
        out = bash("unzip %s -d %s" % (zipfile,destination))
        return out.successful()

    @staticmethod
    def extract_targz(file, destination="."):
        out = bash("tar xzf %s -C %s" % (file,destination))
        return out.successful()

    @staticmethod
    def remove_files_recursively_forced(wildcard):
        out = bash("sudo rm -rf %s" % wildcard)
        return out.successful()

    @staticmethod
    def no_files_exist(wildcard):
        out = bash("sudo ls -1 %s | wc -l" % wildcard)
        return out.successful() and out.output_contains_pattern("(\s)*0(\s)*")

    @staticmethod
    def install_build_env_repo(repo, env_name=None):
        return EscalatePermissions.overwrite('/etc/yum.repos.d/os-env.repo', EnvironmentRepoWriter(repo,env_name))

    @staticmethod
    def generate_ssh_keypair(file):
        return bash("ssh-keygen -N '' -f {file} -t rsa -q".format(file=file)).successful()

    @staticmethod
    def can_execute_sudo_without_pwd():
        out = bash("sudo -l")
        return out.successful() and out.output_nonempty() \
            and (out.output_contains_pattern("\(ALL\)(\s)*NOPASSWD:(\s)*ALL")
                or out.output_contains_pattern("User root may run the following commands on this host"))

class ascii_table(object):
    def __init__(self, str, titles=None):
        if not titles:
            self.titles, self.rows = self.__construct(str)
        else:
            self.titles = titles
            self.rows = self.__construct_rows(str)

    def __construct_rows(self, str):
        #used for nova2ools formatted captionless tables
        rows = []
        for line in str.splitlines():
            rows.append(line.split())
        Row = namedtuple('Row', self.titles)
        return map(Row._make, rows)

    def __construct(self, str):
        def escape(str):
            str = str.strip()
            str = str.replace(' ', '_')
            if str[0].isdigit():
                str = "_" + str
            return str
        column_titles = None
        rows = []
        for line in str.splitlines():
            if '|' in line:
                row =  map(string.strip , line.strip('|').split('|'))
                if column_titles is None:
                    column_titles = map(escape, row)
                else:
                    rows.append(row)

        Row = namedtuple('Row', column_titles)
        rows = map(Row._make, rows)
        return column_titles, rows

    def select_values(self, from_column, where_column, items_equal_to):
        from_column_number = self.titles.index(from_column)
        where_column_name_number = self.titles.index(where_column)
        return [item[from_column_number] for item in self.rows if item[where_column_name_number] == items_equal_to]

    def select(self, from_columns, predicate):
        """`predicate` is a function that returns true on matched row (row represented as dictlike object)
        """
        return [
            map(lambda x: item._asdict()[x], from_columns)
                for item in self.rows
                    if predicate(item._asdict())
        ]

class expect_spawn(pexpect.spawn):
    def get_output(self, code_override=None):
        text_output = "before:\n{before}\nafter:\n{after}".format(
            before = self.before if isinstance(self.before, basestring) else pformat(self.before, indent=4),
            after = self.after if isinstance(self.after, basestring) else pformat(self.after, indent=4))

        if code_override is not None:
            conf.bash_log(pformat(self.args), code_override, text_output)
            return code_override, text_output

        if self.isalive():
            conf.bash_log(pformat(self.args), 'Spawned process running: pid={pid}'.format(pid=self.pid), text_output)
            raise pexpect.ExceptionPexpect('Unable to return exit code. Spawned command is still running:\n' + text_output)

        conf.bash_log(pformat(self.args), self.exitstatus, text_output)
        return self.exitstatus, text_output

class expect_run(command_output):
    def __init__(self, cmdline):
        output = self.__execute(cmdline)
        super(expect_run,self).__init__(output)

    def __execute(self, cmd):
        text, status = pexpect.run(cmd,withexitstatus=True)
        conf.bash_log(cmd, status, text)
        return status, text

class ssh(command_output):
    def __init__(self, host, command=None, user=None, key=None, password=None):

        options='-q -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
        user_prefix = '' if user is None else '%s@' % user

        if key is not None: options += ' -i %s' % key

        cmd = "ssh {options} {user_prefix}{host} {command}".format(options=options,
                                                                   user_prefix=user_prefix,
                                                                   host=host,
                                                                   command=command)

        conf.log(conf.get_bash_log_file(),cmd)

        if password is None:
            super(ssh,self).__init__(bash(cmd).output)
        else:
            super(ssh,self).__init__(self.__use_expect(cmd, password))

    def __use_expect(self, cmd, password):
        spawned = expect_spawn(cmd)
        triggered_index = spawned.expect([pexpect.TIMEOUT, pexpect.EOF, 'password:'])
        if triggered_index == 0:
            return spawned.get_output(-1)
        elif triggered_index == 1:
            return spawned.get_output(-1)

        spawned.sendline(password)
        triggered_index = spawned.expect([pexpect.EOF, pexpect.TIMEOUT])
        if triggered_index == 1:
            spawned.terminate(force=True)

        return spawned.get_output()


class networking(object):

    class http(object):
        @staticmethod
        def probe(url):
            return bash('curl --silent --head %s | grep "200 OK"' % url).successful()

        @staticmethod
        def get(url, destination="."):
            return bash('wget -nv --directory-prefix="%s" %s' % (destination, url)).successful()

        @staticmethod
        def basename(url):
            return os.path.basename(urlparse(url).path)

    class icmp(object):
        @staticmethod
        def probe(ip, timeout):
            start_time = datetime.now()
            while (datetime.now() - start_time).seconds < int(timeout):
                if bash("ping -c3 %s" % ip).successful():
                    return True
            return False

    class nmap(object):
        @staticmethod
        def open_port_serves_protocol(host, port, proto, timeout):
            start_time = datetime.now()
            while(datetime.now() - start_time).seconds < int(timeout):
                if bash('nmap -PN -p %s --open -sV %s | ' \
                        'grep -iE "open.*%s"' % (port, host, proto)).successful():
                    return True

    class ifconfig(object):
        @staticmethod
        def interface_exists(name):
            return bash('sudo ifconfig %s' % name).successful()

        @staticmethod
        def set(interface, options):
            return bash('sudo ifconfig {interface} {options}'.format(interface=interface, options=options)).successful()



    class brctl(object):
        @staticmethod
        def create_bridge(name):
            return bash('sudo brctl addbr %s' % name).successful()

        @staticmethod
        def delete_bridge(name):
            return networking.ifconfig.set(name, 'down') and bash('sudo brctl delbr %s' % name).successful()

        @staticmethod
        def add_interface(bridge, interface):
            return bash('sudo brctl addif {bridge} {interface}'.format(bridge=bridge, interface=interface)).successful()

    class ip(object):
        class addr(object):
            @staticmethod
            def show(param_string):
                return bash('sudo ip addr show %s' % param_string)


#decorator for performing action on step failure
def onfailure(*triggers):
    def decorate(fcn):
        def wrap(*args, **kwargs):
            try:
                retval = fcn(*args, **kwargs)
            except:
                for trigger in triggers:
                    trigger()
                raise
            return retval
        return wrap

    return decorate


class debug(object):
    @staticmethod
    def current_bunch_path():
        return get_current_bunch_dir()

    class save(object):
        @staticmethod
        def file(src):
            dst = os.path.basename(src)
            dst = os.path.join(debug.current_bunch_path(), dst if os.path.splitext(dst)[1] == '.log' else dst + ".log")
            def saving_function():
                bash("sudo dd if={src} of={dst}".format(src=src,dst=dst))
            return saving_function

        @staticmethod
        def command_output(command, file_to_save):
            def command_output_function():
                dst = os.path.join(debug.current_bunch_path(),file_to_save)
                conf.log(dst, bash(command).output_text())
            return command_output_function

        @staticmethod
        def nova_conf():
            debug.save.file('/etc/nova/nova.conf')()

        @staticmethod
        def log(logfile):
            src = logfile if os.path.isabs(logfile) else os.path.join('/var/log', logfile)
            return debug.save.file(src)


