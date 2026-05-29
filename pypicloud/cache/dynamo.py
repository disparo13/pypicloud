""" Store package data in DynamoDB """
import json
import logging
from collections import defaultdict
from datetime import datetime

from pkg_resources import parse_version
from pyramid.settings import asbool, aslist
from pynamodb.attributes import (
    MapAttribute,
    UnicodeAttribute,
    UTCDateTimeAttribute,
)
from pynamodb.exceptions import DoesNotExist
from pynamodb.indexes import AllProjection, GlobalSecondaryIndex
from pynamodb.models import Model
from pynamodb.constants import PAY_PER_REQUEST_BILLING_MODE

from pypicloud.dateutil import UTC, utcnow
from pypicloud.models import Package

from .base import ICache

LOG = logging.getLogger(__name__)


class _JSONAttribute(UnicodeAttribute):
    """Store an arbitrary JSON-serialisable dict as a DynamoDB string."""

    def serialize(self, value):
        if value is None:
            return None
        return json.dumps(value, separators=(",", ":"))

    def deserialize(self, value):
        if not value:
            return {}
        return json.loads(value)


class _NameIndex(GlobalSecondaryIndex):
    """GSI on the *name* field, replacing flywheel's GlobalIndex('name-index', 'name')."""

    class Meta:
        index_name = "name-index"
        projection = AllProjection()
        billing_mode = PAY_PER_REQUEST_BILLING_MODE

    name = UnicodeAttribute(hash_key=True)


class DynamoPackage(Package, Model):
    """Python package stored in DynamoDB."""

    class Meta:
        table_name = "DynamoPackage"
        region = "us-east-1"

    filename = UnicodeAttribute(hash_key=True)
    name = UnicodeAttribute()
    version = UnicodeAttribute()
    last_modified = UTCDateTimeAttribute()
    summary = UnicodeAttribute(null=True)
    data = _JSONAttribute(null=True)
    name_index = _NameIndex()

    def __init__(self, *args, **kwargs):
        # When PynamoDB reconstructs instances from DynamoDB responses it calls
        # _from_raw_data which bypasses __init__ entirely, so this path is only
        # reached for user-created instances (via ICache.new_package).
        Model.__init__(self)  # initialise PynamoDB internal state / defaults
        Package.__init__(self, *args, **kwargs)  # normalise fields
        if not self.summary:
            self.summary = None


class PackageSummary(Model):
    """Aggregate data about packages."""

    class Meta:
        table_name = "PackageSummary"
        region = "us-east-1"

    name = UnicodeAttribute(hash_key=True)
    summary = UnicodeAttribute(null=True)
    last_modified = UTCDateTimeAttribute()

    def __init__(self, package):
        super().__init__()
        self.name = package.name
        self.last_modified = package.last_modified.replace(tzinfo=UTC)
        self.summary = package.summary or None

    def __json__(self):
        return {
            "name": self.name,
            "summary": self.summary,
            "last_modified": self.last_modified,
        }


def _apply_meta(region, host_url, access_key, secret_key):
    """Push connection settings onto the PynamoDB model Meta classes."""
    for model_cls in (DynamoPackage, PackageSummary):
        if region:
            model_cls.Meta.region = region
        if host_url:
            model_cls.Meta.host = host_url
        if access_key:
            model_cls.Meta.aws_access_key_id = access_key
        if secret_key:
            model_cls.Meta.aws_secret_access_key = secret_key


class DynamoCache(ICache):
    """Caching database that uses DynamoDB."""

    def __init__(self, request=None, graceful_reload=False, **kwargs):
        super().__init__(request, **kwargs)
        self.graceful_reload = graceful_reload

    def new_package(self, *args, **kwargs):
        return DynamoPackage(*args, **kwargs)

    @classmethod
    def configure(cls, settings):
        kwargs = super().configure(settings)

        access_key = settings.get("db.aws_access_key_id")
        secret_key = settings.get("db.aws_secret_access_key")
        region = settings.get("db.region_name")
        host = settings.get("db.host")
        port = int(settings.get("db.port", 8000))
        secure = asbool(settings.get("db.secure", False))
        namespace = settings.get("db.namespace", "")
        graceful_reload = asbool(settings.get("db.graceful_reload", False))

        tablenames = aslist(settings.get("db.tablenames", []))
        if tablenames:
            if len(tablenames) != 2:
                raise ValueError("db.tablenames must be a 2-element list")
            DynamoPackage.Meta.table_name = tablenames[0]
            PackageSummary.Meta.table_name = tablenames[1]
        elif namespace:
            DynamoPackage.Meta.table_name = "%s.DynamoPackage" % namespace
            PackageSummary.Meta.table_name = "%s.PackageSummary" % namespace

        if host is not None:
            scheme = "https" if secure else "http"
            host_url = "%s://%s:%d" % (scheme, host, port)
        elif region is not None:
            host_url = None
        else:
            raise ValueError("Must specify either db.region_name or db.host!")

        _apply_meta(region, host_url, access_key, secret_key)

        LOG.info("Checking if DynamoDB tables exist")
        for model_cls in (DynamoPackage, PackageSummary):
            if not model_cls.exists():
                model_cls.create_table(
                    wait=True, billing_mode=PAY_PER_REQUEST_BILLING_MODE
                )

        kwargs["graceful_reload"] = graceful_reload
        return kwargs

    def fetch(self, filename):
        try:
            return DynamoPackage.get(filename)
        except DoesNotExist:
            return None

    def all(self, name):
        return sorted(DynamoPackage.name_index.query(name), reverse=True)

    def distinct(self):
        return sorted({s.name for s in PackageSummary.scan()})

    def summary(self):
        return [s.__json__() for s in sorted(PackageSummary.scan(), key=lambda s: s.name)]

    def clear(self, package):
        package.delete()
        self._maybe_delete_summary(package.name)

    def _maybe_delete_summary(self, package_name):
        """Delete the PackageSummary if no packages with that name remain."""
        remaining = list(DynamoPackage.name_index.query(package_name, limit=1))
        if not remaining:
            LOG.info("Removing package summary %s", package_name)
            try:
                PackageSummary.get(package_name).delete()
            except DoesNotExist:
                pass

    def clear_all(self):
        # NOTE: unlike the flywheel implementation, throughput settings are not
        # preserved across the delete/recreate cycle.  Tables are recreated
        # with PAY_PER_REQUEST billing.  If you rely on provisioned throughput,
        # restore it manually after calling clear_all().
        for model_cls in (DynamoPackage, PackageSummary):
            if model_cls.exists():
                model_cls.delete_table()
        DynamoPackage.create_table(wait=True, billing_mode=PAY_PER_REQUEST_BILLING_MODE)
        PackageSummary.create_table(wait=True, billing_mode=PAY_PER_REQUEST_BILLING_MODE)

    def save(self, package):
        summary = PackageSummary(package)
        package.save()
        summary.save()

    def reload_from_storage(self, clear=True):
        if not self.graceful_reload:
            return super().reload_from_storage(clear)
        LOG.info("Rebuilding cache from storage")
        start = utcnow()

        s1 = set(self.storage.list(self.new_package))
        c1 = set(DynamoPackage.scan())

        missing = s1 - c1
        if missing:
            LOG.info("Adding %d missing packages to cache", len(missing))
            with DynamoPackage.batch_write() as batch:
                for pkg in missing:
                    batch.save(pkg)

        extra1 = [p for p in (c1 - s1) if p.last_modified < start]
        if extra1:
            LOG.info("Removing %d extra packages from cache", len(extra1))
            with DynamoPackage.batch_write() as batch:
                for pkg in extra1:
                    batch.delete(pkg)

        s2 = set(self.storage.list(self.new_package))
        extra2 = s1 - s2
        if extra2:
            LOG.info(
                "Removing %d packages from cache that were concurrently "
                "deleted during rebuild",
                len(extra2),
            )
            with DynamoPackage.batch_write() as batch:
                for pkg in extra2:
                    batch.delete(pkg)
            missing -= extra2

        packages_by_name = defaultdict(list)
        for package in missing:
            package.last_modified = package.last_modified.replace(tzinfo=UTC)
            packages_by_name[package.name].append(package)

        summaries = list(PackageSummary.batch_get(packages_by_name.keys()))
        summaries_by_name = {s.name: s for s in summaries}
        for name, packages in packages_by_name.items():
            if name in summaries_by_name:
                summary = summaries_by_name[name]
            else:
                summary = PackageSummary(packages[0])
                summaries.append(summary)
            for package in packages:
                if package.last_modified > summary.last_modified:
                    summary.last_modified = package.last_modified
                    summary.summary = package.summary
        if summaries:
            LOG.info("Updating %d package summaries", len(summaries))
            with PackageSummary.batch_write() as batch:
                for summary in summaries:
                    batch.save(summary)

        removed = {pkg.name for pkg in extra1} | {pkg.name for pkg in extra2}
        for name in removed:
            self._maybe_delete_summary(name)

    def check_health(self):
        try:
            list(PackageSummary.scan(limit=1))
        except Exception as e:
            return False, str(e)
        return True, ""

