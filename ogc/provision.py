# pylint: disable=wrong-import-position
# pylint: disable=wrong-import-order
import sys
import uuid

from gevent import monkey
from retry.api import retry_call

from ogc.cache import Cache
from ogc.enums import CLOUD_IMAGE_MAP
from ogc.exceptions import ProvisionDeployerException, ProvisionException

monkey.patch_all()
from libcloud.compute.base import NodeAuthSSHKey
from libcloud.compute.deployment import MultiStepDeployment
from libcloud.compute.providers import get_driver
from libcloud.compute.ssh import ParamikoSSHClient
from libcloud.compute.types import Provider
from scp import SCPClient


class ProvisionResult:
    def __init__(self, deploy, deploy_steps_results, layout, ssh_private_key):
        self.deploy = deploy
        self.layout = layout
        self.steps = deploy_steps_results.steps
        self.username = layout.username
        self.ssh_private_key = ssh_private_key

    def render(self, msg_cb):
        msg_cb("Provision Result: ")
        for step in self.steps:
            msg_cb(f"  - [{step.exit_status}]: {step}")
        msg_cb("Connection Information: ")
        msg_cb(f"  - Node: {self.deploy.name} [{self.deploy.state}]")
        msg_cb(
            f"  - ssh -i {self.ssh_private_key} {self.username}@{self.deploy.public_ips[0]}"
        )
        msg_cb("")


class BaseProvisioner:
    def __init__(self, env, **kwargs):
        self._args = kwargs
        self.env = env
        self.uuid = f"ogc-{str(uuid.uuid4())[:8]}"
        self.provisioner = self.connect()

    @property
    def options(self):
        raise NotImplementedError()

    def connect(self):
        raise NotImplementedError()

    def create(self, *args, **kwargs):
        raise NotImplementedError()

    def destroy(self, node):
        return self.provisioner.destroy_node(node)

    def sizes(self, constraints):
        _sizes = self.provisioner.list_sizes()

        explicit_constraints = any(x in constraints for x in ["cores", "disk", "mem"])
        if not explicit_constraints:
            return [size for size in _sizes if size.id == constraints]
        raise ProvisionException(f"Could not locate instance size for {constraints}")

    def image(self, runs_on):
        """Gets a single image from registry of provider"""
        return self.provisioner.get_image(runs_on)

    def images(self, **kwargs):
        return self.provisioner.list_images(**kwargs)

    def _create_node(self, **kwargs):
        _opts = kwargs.copy()
        layout = None
        if "ogc_layout" in _opts:
            layout = _opts["ogc_layout"]
            del _opts["ogc_layout"]

        cache = None
        if "ogc_cache" in _opts:
            cache = _opts["ogc_cache"]
            del _opts["ogc_cache"]

        ssh_creds = None
        if "ogc_ssh" in _opts:
            ssh_creds = _opts["ogc_ssh"]
            del _opts["ogc_ssh"]

        env = None
        if "ogc_env" in _opts:
            env = _opts["ogc_env"]
            del _opts["ogc_env"]

        node = self.provisioner.create_node(**_opts)
        node = self.provisioner.wait_until_running(
            nodes=[node], wait_period=5, timeout=300
        )[0]

        # Store some useful metadata to be used in arbitrary scripts
        metadata = {
            "host": node[1][0],
            "username": layout.username,
            "layout": layout,
            "ssh_public_key": ssh_creds.public,
            "ssh_private_key": ssh_creds.private,
            "node": node[0],
            "uuid": self.uuid,
            "env": env,
        }
        cache.save(layout.name, metadata)
        return {"node": node, "layout": layout, "deployer": Deployer(layout.name)}

    def node(self, **kwargs):
        return self.provisioner.list_nodes(**kwargs)

    def create_keypair(self, ssh):
        return self.provisioner.import_key_pair_from_file(
            name=self.uuid, key_file_path=str(ssh.public)
        )

    def get_key_pair(self, name):
        return self.provisioner.get_key_pair(name)

    def delete_key_pair(self, key_pair):
        retry_call(
            self.provisioner.delete_key_pair, fargs=[key_pair], backoff=3, tries=15
        )

    def list_key_pairs(self):
        return self.provisioner.list_key_pairs()

    def __repr__(self):
        raise NotImplementedError()


class AWSProvisioner(BaseProvisioner):
    """AWS Provisioner

    Environment variables required:
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    """

    @property
    def options(self):
        return {
            "key": self.env.get("AWS_ACCESS_KEY_ID", None),
            "secret": self.env.get("AWS_SECRET_ACCESS_KEY", None),
            "region": self.env.get("AWS_REGION", "us-east-2"),
        }

    def connect(self):
        aws = get_driver(Provider.EC2)
        return aws(**self.options)

    def image(self, runs_on):
        if runs_on.startswith("ami-"):
            _runs_on = runs_on
        else:
            _runs_on = CLOUD_IMAGE_MAP["aws"].get(runs_on)
        return super().image(_runs_on)

    def create(self, layout, ssh, cache, env, msg_cb, **kwargs):
        auth = NodeAuthSSHKey(ssh.public.read_text())
        image = self.image(layout.runs_on)
        if not image and not layout.username:
            raise ProvisionException(
                f"Could not locate AMI and/or username for: {layout.runs_on}"
            )
        size = self.sizes(layout.constraints)
        if size and len(size) > 0:
            size = size[0]

        opts = dict(
            name=f"ogc-{layout.name}",
            image=image,
            size=size,
            auth=auth,
            ex_securitygroup="e2e",
            ex_spot=True,
            ex_terminate_on_shutdown=True,
            ogc_layout=layout,
            ogc_cache=cache,
            ogc_ssh=ssh,
            ogc_env=env,
        )
        return self._create_node(**opts)

    def node(self, **kwargs):
        instance_id = None
        if "instance_id" in kwargs:
            instance_id = kwargs["instance_id"]
        _nodes = super().node(ex_node_ids=[instance_id])
        if _nodes:
            return _nodes[0]
        raise ProvisionException("Unable to get node information")

    def __repr__(self):
        return f"<AWSProvisioner [{self.options['region']}]>"


class GCEProvisioner(BaseProvisioner):
    """Provides abstraction for the GCE provisioner

    Environment variables required:
    GOOGLE_APPLICATION_SERVICE_ACCOUNT: Service user_id associated with credentials
    GOOGLE_APPLICATION_CREDENTIALS: Credentials file for the service account
    """

    @property
    def options(self):
        return {
            "user_id": self.env.get("GOOGLE_APPLICATION_SERVICE_ACCOUNT", None),
            "key": self.env.get("GOOGLE_APPLICATION_CREDENTIALS", None),
            "project": self.env.get("GOOGLE_PROJECT", None),
            "datacenter": self.env.get("GOOGLE_DATACENTER", None),
        }

    def connect(self):
        gce = get_driver(Provider.GCE)
        return gce(**self.options)

    def image(self, runs_on):
        _runs_on = CLOUD_IMAGE_MAP["google"].get(runs_on)
        return super().image(_runs_on)

    def __repr__(self):
        return f"<GCEProvisioner [{self.options['datacenter']}]>"


def choose_provisioner(name, env, **kwargs):
    choices = {"aws": AWSProvisioner, "google": GCEProvisioner}
    provisioner = choices[name]
    return provisioner(env, **kwargs)


class Deployer:
    def __init__(self, name):
        self.cache = Cache()
        if self.cache.exists(name):
            self.cache = self.cache.load(name)
        else:
            raise ProvisionDeployerException(f"Could not locate cache for {name}")
        self.layout = self.cache["layout"]
        engine = choose_provisioner(self.layout.provider, env=self.cache["env"])
        self.node = engine.node(instance_id=self.cache["node"].id)
        self._ssh_client = ParamikoSSHClient(
            self.cache["host"],
            username=self.cache["username"],
            key=str(self.cache["ssh_private_key"]),
            timeout=300,
        )
        retry_call(self._ssh_client.connect, tries=15, delay=5)

    def _progress(self, filename, size, sent, peername):
        if isinstance(filename, bytes):
            filename = filename.decode("utf8")
        sys.stdout.write(
            "[%s:%s] %s progress: %.2f%%                           \r"
            % (self.layout.name, peername[0], filename, float(sent) / float(size) * 100)
        )

    def run(self, metadata, msg_cb):
        msg_cb(
            f"Establishing connection [{self.cache['host']}] [{self.cache['username']}] [{str(self.cache['ssh_private_key'])}]"
        )
        steps = [step.render(metadata) for step in self.layout.steps]
        if steps:
            msg_cb(f"Executing deployment actions for {self.layout.name}")
            msd = MultiStepDeployment(steps)
            msd.run(self.node, self._ssh_client)
            return ProvisionResult(
                self.node, msd, self.layout, self.cache["ssh_private_key"]
            )
        msg_cb("No deployment actions listed, skipping.")
        return None

    def put(self, src, dst, msg_cb):
        scp = SCPClient(self._ssh_client._get_transport(), progress4=self._progress)
        if src.is_dir():
            msg_cb(f"Uploading directory contents of [{src}] to [{dst}]")
            scp.put(str(src), remote_path=str(dst), recursive=True)
        else:
            msg_cb(f"Uploading file [{src}] to [{dst}]")
            scp.put(str(src), str(dst))
        scp.close()
        sys.stdout.write("\n")

    def get(self, dst, src, msg_cb):
        scp = SCPClient(self._ssh_client._get_transport(), progress4=self._progress)
        if str(src).endswith("/"):
            msg_cb(f"Downloading destination contents of [{dst}] to [{src}]")
            scp.get(remote_path=str(dst), local_path=str(src), recursive=True)
        else:
            msg_cb(f"Downloading file [{dst}] to [{src}]")
            scp.get(remote_path=str(dst), local_path=str(src))
        scp.close()
        sys.stdout.write("\n")