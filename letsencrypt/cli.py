"""Let's Encrypt command CLI argument processing."""
from __future__ import print_function
import argparse
import glob
import json
import logging
import logging.handlers
import os
import sys
import traceback

import configargparse
import OpenSSL
import six

import letsencrypt

from letsencrypt import constants
from letsencrypt import crypto_util
from letsencrypt import errors
from letsencrypt import interfaces
from letsencrypt import le_util

from letsencrypt.plugins import disco as plugins_disco
import letsencrypt.plugins.selection as plugin_selection

logger = logging.getLogger(__name__)

# Global, to save us from a lot of argument passing within the scope of this module
helpful_parser = None

# For help strings, figure out how the user ran us.
# When invoked from letsencrypt-auto, sys.argv[0] is something like:
# "/home/user/.local/share/letsencrypt/bin/letsencrypt"
# Note that this won't work if the user set VENV_PATH or XDG_DATA_HOME before
# running letsencrypt-auto (and sudo stops us from seeing if they did), so it
# should only be used for purposes where inability to detect letsencrypt-auto
# fails safely

fragment = os.path.join(".local", "share", "letsencrypt")
cli_command = "letsencrypt-auto" if fragment in sys.argv[0] else "letsencrypt"

# Argparse's help formatting has a lot of unhelpful peculiarities, so we want
# to replace as much of it as we can...

# This is the stub to include in help generated by argparse

SHORT_USAGE = """
  {0} [SUBCOMMAND] [options] [-d domain] [-d domain] ...

The Let's Encrypt agent can obtain and install HTTPS/TLS/SSL certificates.  By
default, it will attempt to use a webserver both for obtaining and installing
the cert. Major SUBCOMMANDS are:

  (default) run        Obtain & install a cert in your current webserver
  certonly             Obtain cert, but do not install it (aka "auth")
  install              Install a previously obtained cert in a server
  renew                Renew previously obtained certs that are near expiry
  revoke               Revoke a previously obtained certificate
  rollback             Rollback server configuration changes made during install
  config_changes       Show changes made to server config during installation
  plugins              Display information about installed plugins

""".format(cli_command)

# This is the short help for letsencrypt --help, where we disable argparse
# altogether
USAGE = SHORT_USAGE + """Choice of server plugins for obtaining and installing cert:

  %s
  --standalone      Run a standalone webserver for authentication
  %s
  --webroot         Place files in a server's webroot folder for authentication

OR use different plugins to obtain (authenticate) the cert and then install it:

  --authenticator standalone --installer apache

More detailed help:

  -h, --help [topic]    print this message, or detailed help on a topic;
                        the available topics are:

   all, automation, paths, security, testing, or any of the subcommands or
   plugins (certonly, install, nginx, apache, standalone, webroot, etc)
"""


def usage_strings(plugins):
    """Make usage strings late so that plugins can be initialised late"""
    if "nginx" in plugins:
        nginx_doc = "--nginx           Use the Nginx plugin for authentication & installation"
    else:
        nginx_doc = "(nginx support is experimental, buggy, and not installed by default)"
    if "apache" in plugins:
        apache_doc = "--apache          Use the Apache plugin for authentication & installation"
    else:
        apache_doc = "(the apache plugin is not installed)"
    return USAGE % (apache_doc, nginx_doc), SHORT_USAGE




def set_by_cli(var):
    """
    Return True if a particular config variable has been set by the user
    (CLI or config file) including if the user explicitly set it to the
    default.  Returns False if the variable was assigned a default value.
    """
    detector = set_by_cli.detector
    if detector is None:
        # Setup on first run: `detector` is a weird version of config in which
        # the default value of every attribute is wrangled to be boolean-false
        plugins = plugins_disco.PluginsRegistry.find_all()
        # reconstructed_args == sys.argv[1:], or whatever was passed to main()
        reconstructed_args = helpful_parser.args + [helpful_parser.verb]
        detector = set_by_cli.detector = prepare_and_parse_args(
            plugins, reconstructed_args, detect_defaults=True)
        # propagate plugin requests: eg --standalone modifies config.authenticator
        auth, inst = plugin_selection.cli_plugin_requests(detector)
        detector.authenticator = auth if auth else ""
        detector.installer = inst if inst else ""
        logger.debug("Default Detector is %r", detector)

    try:
        # Is detector.var something that isn't false?
        change_detected = getattr(detector, var)
    except AttributeError:
        logger.warning("Missing default analysis for %r", var)
        return False

    if change_detected:
        return True
    # Special case: we actually want account to be set to "" if the server
    # the account was on has changed
    elif var == "account" and (detector.server or detector.dry_run or detector.staging):
        return True
    # Special case: vars like --no-redirect that get set True -> False
    # default to None; False means they were set
    elif var in detector.store_false_vars and change_detected is not None:
        return True
    else:
        return False
# static housekeeping var
set_by_cli.detector = None


def argparse_type(variable):
    "Return our argparse type function for a config variable (default: str)"
    # pylint: disable=protected-access
    for action in helpful_parser.parser._actions:
        if action.type is not None and action.dest == variable:
            return action.type
    return str


def read_file(filename, mode="rb"):
    """Returns the given file's contents.

    :param str filename: path to file
    :param str mode: open mode (see `open`)

    :returns: absolute path of filename and its contents
    :rtype: tuple

    :raises argparse.ArgumentTypeError: File does not exist or is not readable.

    """
    try:
        filename = os.path.abspath(filename)
        return filename, open(filename, mode).read()
    except IOError as exc:
        raise argparse.ArgumentTypeError(exc.strerror)


def flag_default(name):
    """Default value for CLI flag."""
    # XXX: this is an internal housekeeping notion of defaults before
    # argparse has been set up; it is not accurate for all flags.  Call it
    # with caution.  Plugin defaults are missing, and some things are using
    # defaults defined in this file, not in constants.py :(
    return constants.CLI_DEFAULTS[name]


def config_help(name, hidden=False):
    """Extract the help message for an `.IConfig` attribute."""
    if hidden:
        return argparse.SUPPRESS
    else:
        return interfaces.IConfig[name].__doc__


class SilentParser(object):  # pylint: disable=too-few-public-methods
    """Silent wrapper around argparse.

    A mini parser wrapper that doesn't print help for its
    arguments. This is needed for the use of callbacks to define
    arguments within plugins.

    """
    def __init__(self, parser):
        self.parser = parser

    def add_argument(self, *args, **kwargs):
        """Wrap, but silence help"""
        kwargs["help"] = argparse.SUPPRESS
        self.parser.add_argument(*args, **kwargs)

class HelpfulArgumentParser(object):
    """Argparse Wrapper.

    This class wraps argparse, adding the ability to make --help less
    verbose, and request help on specific subcategories at a time, eg
    'letsencrypt --help security' for security options.

    """

    def __init__(self, args, plugins, detect_defaults=False):

        from letsencrypt import main
        self.VERBS = main.VERBS
        # List of topics for which additional help can be provided
        HELP_TOPICS = ["all", "security",
                       "paths", "automation", "testing"] + list(six.iterkeys(self.VERBS))

        plugin_names = list(six.iterkeys(plugins))
        self.help_topics = HELP_TOPICS + plugin_names + [None]
        usage, short_usage = usage_strings(plugins)
        self.parser = configargparse.ArgParser(
            usage=short_usage,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            args_for_setting_config_path=["-c", "--config"],
            default_config_files=flag_default("config_files"))

        # This is the only way to turn off overly verbose config flag documentation
        self.parser._add_config_file_help = False  # pylint: disable=protected-access
        self.silent_parser = SilentParser(self.parser)

        # This setting attempts to force all default values to things that are
        # pythonically false; it is used to detect when values have been
        # explicitly set by the user, including when they are set to their
        # normal default value
        self.detect_defaults = detect_defaults
        if detect_defaults:
            self.store_false_vars = {}  # vars that use "store_false"

        self.args = args
        self.determine_verb()
        help1 = self.prescan_for_flag("-h", self.help_topics)
        help2 = self.prescan_for_flag("--help", self.help_topics)
        assert max(True, "a") == "a", "Gravity changed direction"
        self.help_arg = max(help1, help2)
        if self.help_arg is True:
            # just --help with no topic; avoid argparse altogether
            print(usage)
            sys.exit(0)
        self.visible_topics = self.determine_help_topics(self.help_arg)
        self.groups = {}       # elements are added by .add_group()

    def parse_args(self):
        """Parses command line arguments and returns the result.

        :returns: parsed command line arguments
        :rtype: argparse.Namespace

        """
        parsed_args = self.parser.parse_args(self.args)
        parsed_args.func = self.VERBS[self.verb]
        parsed_args.verb = self.verb

        # Do any post-parsing homework here

        # we get domains from -d, but also from the webroot map...
        if parsed_args.webroot_map:
            for domain in parsed_args.webroot_map.keys():
                if domain not in parsed_args.domains:
                    parsed_args.domains.append(domain)

        if parsed_args.staging or parsed_args.dry_run:
            if parsed_args.server not in (flag_default("server"), constants.STAGING_URI):
                conflicts = ["--staging"] if parsed_args.staging else []
                conflicts += ["--dry-run"] if parsed_args.dry_run else []
                if not self.detect_defaults:
                    raise errors.Error("--server value conflicts with {0}".format(
                        " and ".join(conflicts)))

            parsed_args.server = constants.STAGING_URI

            if parsed_args.dry_run:
                if self.verb not in ["certonly", "renew"]:
                    raise errors.Error("--dry-run currently only works with the "
                                       "'certonly' or 'renew' subcommands (%r)" % self.verb)
                parsed_args.break_my_certs = parsed_args.staging = True
                if glob.glob(os.path.join(parsed_args.config_dir, constants.ACCOUNTS_DIR, "*")):
                    # The user has a prod account, but might not have a staging
                    # one; we don't want to start trying to perform interactive registration
                    parsed_args.agree_tos = True
                    parsed_args.register_unsafely_without_email = True

        if parsed_args.csr:
            self.handle_csr(parsed_args)

        if self.detect_defaults:  # plumbing
            parsed_args.store_false_vars = self.store_false_vars

        return parsed_args

    def handle_csr(self, parsed_args):
        """
        Process a --csr flag. This needs to happen early enough that the
        webroot plugin can know about the calls to process_domain
        """
        if parsed_args.verb != "certonly":
            raise errors.Error("Currently, a CSR file may only be specified "
                               "when obtaining a new or replacement "
                               "via the certonly command. Please try the "
                               "certonly command instead.")

        try:
            csr = le_util.CSR(file=parsed_args.csr[0], data=parsed_args.csr[1], form="der")
            typ = OpenSSL.crypto.FILETYPE_ASN1
            domains = crypto_util.get_sans_from_csr(csr.data, OpenSSL.crypto.FILETYPE_ASN1)
        except OpenSSL.crypto.Error:
            try:
                e1 = traceback.format_exc()
                typ = OpenSSL.crypto.FILETYPE_PEM
                csr = le_util.CSR(file=parsed_args.csr[0], data=parsed_args.csr[1], form="pem")
                domains = crypto_util.get_sans_from_csr(csr.data, typ)
            except OpenSSL.crypto.Error:
                logger.debug("DER CSR parse error %s", e1)
                logger.debug("PEM CSR parse error %s", traceback.format_exc())
                raise errors.Error("Failed to parse CSR file: {0}".format(parsed_args.csr[0]))
        for d in domains:
            process_domain(parsed_args, d)

        for d in domains:
            sanitised = le_util.enforce_domain_sanity(d)
            if d.lower() != sanitised:
                raise errors.ConfigurationError(
                    "CSR domain {0} needs to be sanitised to {1}.".format(d, sanitised))

        if not domains:
            # TODO: add CN to domains instead:
            raise errors.Error(
                "Unfortunately, your CSR %s needs to have a SubjectAltName for every domain"
                % parsed_args.csr[0])

        parsed_args.actual_csr = (csr, typ)
        csr_domains, config_domains = set(domains), set(parsed_args.domains)
        if csr_domains != config_domains:
            raise errors.ConfigurationError(
                "Inconsistent domain requests:\nFrom the CSR: {0}\nFrom command line/config: {1}"
                .format(", ".join(csr_domains), ", ".join(config_domains)))


    def determine_verb(self):
        """Determines the verb/subcommand provided by the user.

        This function works around some of the limitations of argparse.

        """
        if "-h" in self.args or "--help" in self.args:
            # all verbs double as help arguments; don't get them confused
            self.verb = "help"
            return

        for i, token in enumerate(self.args):
            if token in self.VERBS:
                verb = token
                if verb == "auth":
                    verb = "certonly"
                if verb == "everything":
                    verb = "run"
                self.verb = verb
                self.args.pop(i)
                return

        self.verb = "run"

    def prescan_for_flag(self, flag, possible_arguments):
        """Checks cli input for flags.

        Check for a flag, which accepts a fixed set of possible arguments, in
        the command line; we will use this information to configure argparse's
        help correctly.  Return the flag's argument, if it has one that matches
        the sequence @possible_arguments; otherwise return whether the flag is
        present.

        """
        if flag not in self.args:
            return False
        pos = self.args.index(flag)
        try:
            nxt = self.args[pos + 1]
            if nxt in possible_arguments:
                return nxt
        except IndexError:
            pass
        return True

    def add(self, topic, *args, **kwargs):
        """Add a new command line argument.

        :param str: help topic this should be listed under, can be None for
                    "always documented"
        :param list *args: the names of this argument flag
        :param dict **kwargs: various argparse settings for this argument

        """

        if self.detect_defaults:
            kwargs = self.modify_arg_for_default_detection(self, *args, **kwargs)

        if self.visible_topics[topic]:
            if topic in self.groups:
                group = self.groups[topic]
                group.add_argument(*args, **kwargs)
            else:
                self.parser.add_argument(*args, **kwargs)
        else:
            kwargs["help"] = argparse.SUPPRESS
            self.parser.add_argument(*args, **kwargs)


    def modify_arg_for_default_detection(self, *args, **kwargs):
        """
        Adding an arg, but ensure that it has a default that evaluates to false,
        so that set_by_cli can tell if it was set.  Only called if detect_defaults==True.

        :param list *args: the names of this argument flag
        :param dict **kwargs: various argparse settings for this argument

        :returns: a modified versions of kwargs
        """
        # argument either doesn't have a default, or the default doesn't
        # isn't Pythonically false
        if kwargs.get("default", True):
            arg_type = kwargs.get("type", None)
            if arg_type == int or kwargs.get("action", "") == "count":
                kwargs["default"] = 0
            elif arg_type == read_file or "-c" in args:
                kwargs["default"] = ""
                kwargs["type"] = str
            else:
                kwargs["default"] = ""
            # This doesn't matter at present (none of the store_false args
            # are renewal-relevant), but implement it for future sanity:
            # detect the setting of args whose presence causes True -> False
        if kwargs.get("action", "") == "store_false":
            kwargs["default"] = None
            for var in args:
                self.store_false_vars[var] = True

        return kwargs


    def add_deprecated_argument(self, argument_name, num_args):
        """Adds a deprecated argument with the name argument_name.

        Deprecated arguments are not shown in the help. If they are used
        on the command line, a warning is shown stating that the
        argument is deprecated and no other action is taken.

        :param str argument_name: Name of deprecated argument.
        :param int nargs: Number of arguments the option takes.

        """
        le_util.add_deprecated_argument(
            self.parser.add_argument, argument_name, num_args)

    def add_group(self, topic, **kwargs):
        """

        This has to be called once for every topic; but we leave those calls
        next to the argument definitions for clarity. Return something
        arguments can be added to if necessary, either the parser or an argument
        group.

        """
        if self.visible_topics[topic]:
            #print("Adding visible group " + topic)
            group = self.parser.add_argument_group(topic, **kwargs)
            self.groups[topic] = group
            return group
        else:
            #print("Invisible group " + topic)
            return self.silent_parser

    def add_plugin_args(self, plugins):
        """

        Let each of the plugins add its own command line arguments, which
        may or may not be displayed as help topics.

        """
        for name, plugin_ep in six.iteritems(plugins):
            parser_or_group = self.add_group(name, description=plugin_ep.description)
            #print(parser_or_group)
            plugin_ep.plugin_cls.inject_parser_options(parser_or_group, name)

    def determine_help_topics(self, chosen_topic):
        """

        The user may have requested help on a topic, return a dict of which
        topics to display. @chosen_topic has prescan_for_flag's return type

        :returns: dict

        """
        # topics maps each topic to whether it should be documented by
        # argparse on the command line
        if chosen_topic == "auth":
            chosen_topic = "certonly"
        if chosen_topic == "everything":
            chosen_topic = "run"
        if chosen_topic == "all":
            return dict([(t, True) for t in self.help_topics])
        elif not chosen_topic:
            return dict([(t, False) for t in self.help_topics])
        else:
            return dict([(t, t == chosen_topic) for t in self.help_topics])


def prepare_and_parse_args(plugins, args, detect_defaults=False):
    """Returns parsed command line arguments.

    :param .PluginsRegistry plugins: available plugins
    :param list args: command line arguments with the program name removed

    :returns: parsed command line arguments
    :rtype: argparse.Namespace

    """
    helpful = HelpfulArgumentParser(args, plugins, detect_defaults)

    # --help is automatically provided by argparse
    helpful.add(
        None, "-v", "--verbose", dest="verbose_count", action="count",
        default=flag_default("verbose_count"), help="This flag can be used "
        "multiple times to incrementally increase the verbosity of output, "
        "e.g. -vvv.")
    helpful.add(
        None, "-t", "--text", dest="text_mode", action="store_true",
        help="Use the text output instead of the curses UI.")
    helpful.add(
        None, "-n", "--non-interactive", "--noninteractive",
        dest="noninteractive_mode", action="store_true",
        help="Run without ever asking for user input. This may require "
              "additional command line flags; the client will try to explain "
              "which ones are required if it finds one missing")
    helpful.add(
        None, "--dry-run", action="store_true", dest="dry_run",
        help="Perform a test run of the client, obtaining test (invalid) certs"
             " but not saving them to disk. This can currently only be used"
             " with the 'certonly' subcommand.")
    helpful.add(
        None, "--register-unsafely-without-email", action="store_true",
        help="Specifying this flag enables registering an account with no "
             "email address. This is strongly discouraged, because in the "
             "event of key loss or account compromise you will irrevocably "
             "lose access to your account. You will also be unable to receive "
             "notice about impending expiration or revocation of your "
             "certificates. Updates to the Subscriber Agreement will still "
             "affect you, and will be effective 14 days after posting an "
             "update to the web site.")
    helpful.add(None, "-m", "--email", help=config_help("email"))
    # positional arg shadows --domains, instead of appending, and
    # --domains is useful, because it can be stored in config
    #for subparser in parser_run, parser_auth, parser_install:
    #    subparser.add_argument("domains", nargs="*", metavar="domain")
    helpful.add(None, "-d", "--domains", "--domain", dest="domains",
                metavar="DOMAIN", action=DomainFlagProcessor, default=[],
                help="Domain names to apply. For multiple domains you can use "
                "multiple -d flags or enter a comma separated list of domains "
                "as a parameter.")
    helpful.add_group(
        "automation",
        description="Arguments for automating execution & other tweaks")
    helpful.add(
        "automation", "--keep-until-expiring", "--keep", "--reinstall",
        dest="reinstall", action="store_true",
        help="If the requested cert matches an existing cert, always keep the "
             "existing one until it is due for renewal (for the "
             "'run' subcommand this means reinstall the existing cert)")
    helpful.add(
        "automation", "--expand", action="store_true",
        help="If an existing cert covers some subset of the requested names, "
             "always expand and replace it with the additional names.")
    helpful.add(
        "automation", "--version", action="version",
        version="%(prog)s {0}".format(letsencrypt.__version__),
        help="show program's version number and exit")
    helpful.add(
        "automation", "--force-renewal", "--renew-by-default",
        action="store_true", dest="renew_by_default", help="If a certificate "
             "already exists for the requested domains, renew it now, "
             "regardless of whether it is near expiry. (Often "
             "--keep-until-expiring is more appropriate). Also implies "
             "--expand.")
    helpful.add(
        "automation", "--agree-tos", dest="tos", action="store_true",
        help="Agree to the Let's Encrypt Subscriber Agreement")
    helpful.add(
        "automation", "--account", metavar="ACCOUNT_ID",
        help="Account ID to use")
    helpful.add(
        "automation", "--duplicate", dest="duplicate", action="store_true",
        help="Allow making a certificate lineage that duplicates an existing one "
             "(both can be renewed in parallel)")
    helpful.add(
        "automation", "--os-packages-only", action="store_true",
        help="(letsencrypt-auto only) install OS package dependencies and then stop")
    helpful.add(
        "automation", "--no-self-upgrade", action="store_true",
        help="(letsencrypt-auto only) prevent the letsencrypt-auto script from"
             " upgrading itself to newer released versions")

    helpful.add_group(
        "testing", description="The following flags are meant for "
        "testing purposes only! Do NOT change them, unless you "
        "really know what you're doing!")
    helpful.add(
        "testing", "--debug", action="store_true",
        help="Show tracebacks in case of errors, and allow letsencrypt-auto "
             "execution on experimental platforms")
    helpful.add(
        "testing", "--no-verify-ssl", action="store_true",
        help=config_help("no_verify_ssl"),
        default=flag_default("no_verify_ssl"))
    helpful.add(
        "testing", "--tls-sni-01-port", type=int,
        default=flag_default("tls_sni_01_port"),
        help=config_help("tls_sni_01_port"))
    helpful.add(
        "testing", "--http-01-port", type=int, dest="http01_port",
        default=flag_default("http01_port"), help=config_help("http01_port"))
    helpful.add(
        "testing", "--break-my-certs", action="store_true",
        help="Be willing to replace or renew valid certs with invalid "
             "(testing/staging) certs")
    helpful.add_group(
        "security", description="Security parameters & server settings")
    helpful.add(
        "security", "--rsa-key-size", type=int, metavar="N",
        default=flag_default("rsa_key_size"), help=config_help("rsa_key_size"))
    helpful.add(
        "security", "--redirect", action="store_true",
        help="Automatically redirect all HTTP traffic to HTTPS for the newly "
             "authenticated vhost.", dest="redirect", default=None)
    helpful.add(
        "security", "--no-redirect", action="store_false",
        help="Do not automatically redirect all HTTP traffic to HTTPS for the newly "
             "authenticated vhost.", dest="redirect", default=None)
    helpful.add(
        "security", "--hsts", action="store_true",
        help="Add the Strict-Transport-Security header to every HTTP response."
             " Forcing browser to use always use SSL for the domain."
             " Defends against SSL Stripping.", dest="hsts", default=False)
    helpful.add(
        "security", "--no-hsts", action="store_false",
        help="Do not automatically add the Strict-Transport-Security header"
             " to every HTTP response.", dest="hsts", default=False)
    helpful.add(
        "security", "--uir", action="store_true",
        help="Add the \"Content-Security-Policy: upgrade-insecure-requests\""
             " header to every HTTP response. Forcing the browser to use"
             " https:// for every http:// resource.", dest="uir", default=None)
    helpful.add(
        "security", "--no-uir", action="store_false",
        help=" Do not automatically set the \"Content-Security-Policy:"
        " upgrade-insecure-requests\" header to every HTTP response.",
        dest="uir", default=None)
    helpful.add(
        "security", "--strict-permissions", action="store_true",
        help="Require that all configuration files are owned by the current "
             "user; only needed if your config is somewhere unsafe like /tmp/")

    helpful.add_group(
        "renew", description="The 'renew' subcommand will attempt to renew all"
        " certificates (or more precisely, certificate lineages) you have"
        " previously obtained if they are close to expiry, and print a"
        " summary of the results. By default, 'renew' will reuse the options"
        " used to create obtain or most recently successfully renew each"
        " certificate lineage. You can try it with `--dry-run` first. For"
        " more fine-grained control, you can renew individual lineages with"
        " the `certonly` subcommand.")

    helpful.add_deprecated_argument("--agree-dev-preview", 0)

    _create_subparsers(helpful)
    _paths_parser(helpful)
    # _plugins_parsing should be the last thing to act upon the main
    # parser (--help should display plugin-specific options last)
    _plugins_parsing(helpful, plugins)

    if not detect_defaults:
        global helpful_parser # pylint: disable=global-statement
        helpful_parser = helpful
    return helpful.parse_args()


def _create_subparsers(helpful):
    helpful.add_group("certonly", description="Options for modifying how a cert is obtained")
    helpful.add_group("install", description="Options for modifying how a cert is deployed")
    helpful.add_group("revoke", description="Options for revocation of certs")
    helpful.add_group("rollback", description="Options for reverting config changes")
    helpful.add_group("plugins", description="Plugin options")
    helpful.add(
        None, "--user-agent", default=None,
        help="Set a custom user agent string for the client. User agent strings allow "
             "the CA to collect high level statistics about success rates by OS and "
             "plugin. If you wish to hide your server OS version from the Let's "
             'Encrypt server, set this to "".')
    helpful.add("certonly",
                "--csr", type=read_file,
                help="Path to a Certificate Signing Request (CSR) in DER"
                " format; note that the .csr file *must* contain a Subject"
                " Alternative Name field for each domain you want certified."
                " Currently --csr only works with the 'certonly' subcommand'")
    helpful.add("rollback",
                "--checkpoints", type=int, metavar="N",
                default=flag_default("rollback_checkpoints"),
                help="Revert configuration N number of checkpoints.")
    helpful.add("plugins",
                "--init", action="store_true", help="Initialize plugins.")
    helpful.add("plugins",
                "--prepare", action="store_true", help="Initialize and prepare plugins.")
    helpful.add("plugins",
                "--authenticators", action="append_const", dest="ifaces",
                const=interfaces.IAuthenticator, help="Limit to authenticator plugins only.")
    helpful.add("plugins",
                "--installers", action="append_const", dest="ifaces",
                const=interfaces.IInstaller, help="Limit to installer plugins only.")


def _paths_parser(helpful):
    add = helpful.add
    verb = helpful.verb
    if verb == "help":
        verb = helpful.help_arg
    helpful.add_group(
        "paths", description="Arguments changing execution paths & servers")

    cph = "Path to where cert is saved (with auth --csr), installed from or revoked."
    section = "paths"
    if verb in ("install", "revoke", "certonly"):
        section = verb
    if verb == "certonly":
        add(section, "--cert-path", type=os.path.abspath,
            default=flag_default("auth_cert_path"), help=cph)
    elif verb == "revoke":
        add(section, "--cert-path", type=read_file, required=True, help=cph)
    else:
        add(section, "--cert-path", type=os.path.abspath,
            help=cph, required=(verb == "install"))

    section = "paths"
    if verb in ("install", "revoke"):
        section = verb
    # revoke --key-path reads a file, install --key-path takes a string
    add(section, "--key-path", required=(verb == "install"),
        type=((verb == "revoke" and read_file) or os.path.abspath),
        help="Path to private key for cert installation "
             "or revocation (if account key is missing)")

    default_cp = None
    if verb == "certonly":
        default_cp = flag_default("auth_chain_path")
    add("paths", "--fullchain-path", default=default_cp, type=os.path.abspath,
        help="Accompanying path to a full certificate chain (cert plus chain).")
    add("paths", "--chain-path", default=default_cp, type=os.path.abspath,
        help="Accompanying path to a certificate chain.")
    add("paths", "--config-dir", default=flag_default("config_dir"),
        help=config_help("config_dir"))
    add("paths", "--work-dir", default=flag_default("work_dir"),
        help=config_help("work_dir"))
    add("paths", "--logs-dir", default=flag_default("logs_dir"),
        help="Logs directory.")
    add("paths", "--server", default=flag_default("server"),
        help=config_help("server"))
    # overwrites server, handled in HelpfulArgumentParser.parse_args()
    add("testing", "--test-cert", "--staging", action='store_true', dest='staging',
        help='Use the staging server to obtain test (invalid) certs; equivalent'
             ' to --server ' + constants.STAGING_URI)


def _plugins_parsing(helpful, plugins):
    helpful.add_group(
        "plugins", description="Let's Encrypt client supports an "
        "extensible plugins architecture. See '%(prog)s plugins' for a "
        "list of all installed plugins and their names. You can force "
        "a particular plugin by setting options provided below. Running "
        "--help <plugin_name> will list flags specific to that plugin.")
    helpful.add(
        "plugins", "-a", "--authenticator", help="Authenticator plugin name.")
    helpful.add(
        "plugins", "-i", "--installer", help="Installer plugin name (also used to find domains).")
    helpful.add(
        "plugins", "--configurator", help="Name of the plugin that is "
        "both an authenticator and an installer. Should not be used "
        "together with --authenticator or --installer.")
    helpful.add("plugins", "--apache", action="store_true",
                help="Obtain and install certs using Apache")
    helpful.add("plugins", "--nginx", action="store_true",
                help="Obtain and install certs using Nginx")
    helpful.add("plugins", "--standalone", action="store_true",
                help='Obtain certs using a "standalone" webserver.')
    helpful.add("plugins", "--manual", action="store_true",
                help='Provide laborious manual instructions for obtaining a cert')
    helpful.add("plugins", "--webroot", action="store_true",
                help='Obtain certs by placing files in a webroot directory.')

    # things should not be reorder past/pre this comment:
    # plugins_group should be displayed in --help before plugin
    # specific groups (so that plugins_group.description makes sense)

    helpful.add_plugin_args(plugins)

    # These would normally be a flag within the webroot plugin, but because
    # they are parsed in conjunction with --domains, they live here for
    # legibility. helpful.add_plugin_ags must be called first to add the
    # "webroot" topic
    helpful.add("webroot", "-w", "--webroot-path", default=[], action=WebrootPathProcessor,
                help="public_html / webroot path. This can be specified multiple times to "
                     "handle different domains; each domain will have the webroot path that"
                     " preceded it.  For instance: `-w /var/www/example -d example.com -d "
                     "www.example.com -w /var/www/thing -d thing.net -d m.thing.net`")
    # --webroot-map still has some awkward properties, so it is undocumented
    helpful.add("webroot", "--webroot-map", default={}, action=WebrootMapProcessor,
                help="JSON dictionary mapping domains to webroot paths; this "
                     "implies -d for each entry. You may need to escape this "
                     "from your shell. E.g.: --webroot-map "
                     """'{"eg1.is,m.eg1.is":"/www/eg1/", "eg2.is":"/www/eg2"}' """
                     "This option is merged with, but takes precedence over, "
                     "-w / -d entries. At present, if you put webroot-map in "
                     "a config file, it needs to be on a single line, like: "
                     'webroot-map = {"example.com":"/var/www"}.')


class WebrootPathProcessor(argparse.Action):  # pylint: disable=missing-docstring
    def __init__(self, *args, **kwargs):
        self.domain_before_webroot = False
        argparse.Action.__init__(self, *args, **kwargs)

    def __call__(self, parser, args, webroot, option_string=None):
        """
        Keep a record of --webroot-path / -w flags during processing, so that
        we know which apply to which -d flags
        """
        if not args.webroot_path:      # first -w flag encountered
            # if any --domain flags preceded the first --webroot-path flag,
            # apply that webroot path to those; subsequent entries in
            # args.webroot_map are filled in by cli.DomainFlagProcessor
            if args.domains:
                self.domain_before_webroot = True
                for d in args.domains:
                    args.webroot_map.setdefault(d, webroot)
        elif self.domain_before_webroot:
            # FIXME if you set domains in a args file, you should get a different error
            # here, pointing you to --webroot-map
            raise errors.Error("If you specify multiple webroot paths, one of "
                               "them must precede all domain flags")
        args.webroot_path.append(webroot)


def process_domain(args_or_config, domain_arg, webroot_path=None):
    """
    Process a new -d flag, helping the webroot plugin construct a map of
    {domain : webrootpath} if -w / --webroot-path is in use

    :param args_or_config: may be an argparse args object, or a NamespaceConfig object
    :param str domain_arg: a string representing 1+ domains, eg: "eg.is, example.com"
    :param str webroot_path: (optional) the webroot_path for these domains

    """
    webroot_path = webroot_path if webroot_path else args_or_config.webroot_path

    for domain in (d.strip() for d in domain_arg.split(",")):
        domain = le_util.enforce_domain_sanity(domain)
        if domain not in args_or_config.domains:
            args_or_config.domains.append(domain)
            # Each domain has a webroot_path of the most recent -w flag
            # unless it was explicitly included in webroot_map
            if webroot_path:
                args_or_config.webroot_map.setdefault(domain, webroot_path[-1])


class WebrootMapProcessor(argparse.Action):  # pylint: disable=missing-docstring
    def __call__(self, parser, args, webroot_map_arg, option_string=None):
        webroot_map = json.loads(webroot_map_arg)
        for domains, webroot_path in six.iteritems(webroot_map):
            process_domain(args, domains, [webroot_path])


class DomainFlagProcessor(argparse.Action):  # pylint: disable=missing-docstring
    def __call__(self, parser, args, domain_arg, option_string=None):
        """Just wrap process_domain in argparseese."""
        process_domain(args, domain_arg)
