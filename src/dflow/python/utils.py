import os
import shutil
import signal
import tarfile
import traceback
import uuid
from pathlib import Path
from typing import Dict, List, Set, Union

from ..common import jsonpickle
from ..config import config
from ..utils import (artifact_classes, assemble_path_object,
                     catalog_of_local_artifact, convert_dflow_list, copy_file,
                     expand, flatten, randstr, remove_empty_dir_tag)
from .opio import (Artifact, BigParameter, HDF5Dataset, HDF5Datasets,
                   NestedDict, Parameter)


def get_slices(path_object, slices):
    if isinstance(slices, int):
        return path_object[slices]
    elif isinstance(slices, str):
        tmp = path_object
        for f in slices.split("."):
            if isinstance(tmp, dict):
                tmp = tmp[f]
            elif isinstance(tmp, list):
                tmp = tmp[int(f)]
        return tmp
    elif isinstance(slices, list):
        return [get_slices(path_object, i) for i in slices]
    return path_object


def handle_input_artifact(name, sign, slices=None, data_root="/tmp",
                          sub_path=None, n_parts=None, keys_of_parts=None,
                          path=None, prefix=None):
    root = None
    if n_parts is not None:
        path_object = []
        for i in range(n_parts):
            art_path = '%s/inputs/artifacts/dflow_%s_%s' % (data_root, name, i)
            if config["detect_empty_dir"]:
                remove_empty_dir_tag(art_path)
            po = assemble_path_object(art_path)
            if prefix is not None:
                po = get_slices(po, prefix[i])
            if not po:
                path_object.append(art_path)
            elif isinstance(po, list):
                path_object += po
            else:
                path_object.append(po)
    elif keys_of_parts is not None:
        path_object = {}
        for i in keys_of_parts:
            art_path = '%s/inputs/artifacts/dflow_%s_%s' % (data_root, name, i)
            if config["detect_empty_dir"]:
                remove_empty_dir_tag(art_path)
            po = assemble_path_object(art_path)
            if prefix is not None:
                po = get_slices(po, prefix[i])
            if not po:
                path_object[i] = art_path
            else:
                path_object[i] = po
        path_object = expand(path_object)
    else:
        art_path = '%s/inputs/artifacts/%s' % (data_root, name) \
            if path is None else path
        if sub_path is not None:
            art_path = os.path.join(art_path, sub_path)
        root = art_path
        if not os.path.exists(art_path):  # for optional artifact
            return None
        if config["detect_empty_dir"]:
            remove_empty_dir_tag(art_path)
        path_object = assemble_path_object(art_path)
        path_object = get_slices(path_object, prefix)

    path_object = get_slices(path_object, slices)

    sign_type = sign.type
    if getattr(sign_type, "__origin__", None) == Union:
        args = sign_type.__args__
        if HDF5Datasets in args:
            if isinstance(path_object, list) and all([isinstance(
                    p, str) and p.endswith(".h5") for p in path_object]):
                sign_type = HDF5Datasets
            elif args[0] == HDF5Datasets:
                sign_type = args[1]
            elif args[1] == HDF5Datasets:
                sign_type = args[0]

    if sign_type == HDF5Datasets:
        import h5py
        assert isinstance(path_object, list)
        res = None
        for path in path_object:
            f = h5py.File(path, "r")
            datasets = {k: HDF5Dataset(f[k]) for k in f.keys()}
            datasets = expand(datasets)
            if isinstance(datasets, list):
                if res is None:
                    res = []
                res += datasets
            elif isinstance(datasets, dict):
                if res is None:
                    res = {}
                res.update(datasets)
    if sign_type in [str, Path]:
        if path_object is None or isinstance(path_object, str):
            res = path_object
        elif isinstance(path_object, list) and len(path_object) == 1 and (
            path_object[0] is None or isinstance(path_object[0], str)) \
                and sign.sub_path:
            res = path_object[0]
        else:
            res = art_path
        res = path_or_none(res) if sign_type == Path else res
    elif sign_type in [List[str], List[Path], Set[str], Set[Path]]:
        if path_object is None:
            return None
        elif isinstance(path_object, str):
            res = [path_object]
        elif isinstance(path_object, list) and all([
                p is None or isinstance(p, str) for p in path_object]):
            res = path_object
        else:
            res = list(flatten(path_object).values())

        if sign_type == List[str]:
            pass
        elif sign_type == List[Path]:
            res = path_or_none(res)
        elif sign_type == Set[str]:
            res = set(res)
        else:
            res = set(path_or_none(res))
    elif sign_type in [Dict[str, str], NestedDict[str]]:
        res = path_object
    elif sign_type in [Dict[str, Path], NestedDict[Path]]:
        res = path_or_none(path_object)

    if res is None:
        return None

    _cls = res.__class__
    res = artifact_classes[_cls](res)
    res.art_root = root
    return res


def path_or_none(p):
    if p is None:
        return None
    elif isinstance(p, list):
        return [path_or_none(i) for i in p]
    elif isinstance(p, dict):
        return {k: path_or_none(v) for k, v in p.items()}
    else:
        return Path(p)


def handle_input_parameter(name, value, sign, slices=None, data_root="/tmp"):
    if "dflow_list_item" in value:
        dflow_list = []
        for item in jsonpickle.loads(value):
            dflow_list += jsonpickle.loads(item)
        obj = convert_dflow_list(dflow_list)
    elif isinstance(sign, BigParameter) and config["mode"] != "debug":
        with open(data_root + "/inputs/parameters/" + name, "r") as f:
            content = jsonpickle.loads(f.read())
            obj = content
    else:
        if isinstance(sign, (Parameter, BigParameter)):
            sign = sign.type
        if sign == str and slices is None:
            obj = value
        else:
            obj = jsonpickle.loads(value)

    if slices is not None:
        assert isinstance(
            obj, list), "Only parameters of type list can be sliced, while %s"\
            " is not list" % obj
        if isinstance(slices, list):
            obj = [obj[i] for i in slices]
        else:
            obj = obj[slices]

    return obj


def slice_to_dir(slice):
    return str(slice).replace(".", "/")


def handle_output_artifact(name, value, sign, slices=None, data_root="/tmp",
                           create_dir=False):
    path_list = []
    if sign.type == HDF5Datasets:
        import h5py
        os.makedirs(data_root + '/outputs/artifacts/' + name, exist_ok=True)
        h5_name = "%s.h5" % uuid.uuid4()
        h5_path = '%s/outputs/artifacts/%s/%s' % (data_root, name, h5_name)
        with h5py.File(h5_path, "w") as f:
            for s, v in flatten(value).items():
                if isinstance(v, Path):
                    if v.is_file():
                        try:
                            data = v.read_text(encoding="utf-8")
                            dtype = "utf-8"
                        except Exception:
                            import numpy as np
                            data = np.void(v.read_bytes())
                            dtype = "binary"
                        d = f.create_dataset(s, data=data)
                        d.attrs["type"] = "file"
                        d.attrs["path"] = str(v)
                        d.attrs["dtype"] = dtype
                    elif v.is_dir():
                        tgz_path = Path("%s.tgz" % v)
                        tf = tarfile.open(tgz_path, "w:gz", dereference=True)
                        tf.add(v)
                        tf.close()
                        import numpy as np
                        d = f.create_dataset(s, data=np.void(
                            tgz_path.read_bytes()))
                        d.attrs["type"] = "dir"
                        d.attrs["path"] = str(v)
                        d.attrs["dtype"] = "binary"
                else:
                    d = f.create_dataset(s, data=v)
                    d.attrs["type"] = "data"
                    if isinstance(v, str):
                        d.attrs["dtype"] = "utf-8"
        path_list.append({"dflow_list_item": h5_name, "order": slices or 0})
    if sign.type in [str, Path]:
        os.makedirs(data_root + '/outputs/artifacts/' + name, exist_ok=True)
        if slices is None:
            slices = 0
        path_list.append(copy_results_and_return_path_item(
            value, name, slices, data_root,
            slice_to_dir(slices) if create_dir else None))
    elif sign.type in [List[str], List[Path], Set[str], Set[Path]]:
        os.makedirs(data_root + '/outputs/artifacts/' + name, exist_ok=True)
        if slices is not None:
            if isinstance(slices, int):
                for path in value:
                    path_list.append(copy_results_and_return_path_item(
                        path, name, slices, data_root,
                        slice_to_dir(slices) if create_dir else None))
            else:
                assert len(slices) == len(value)
                for path, s in zip(value, slices):
                    if isinstance(path, list):
                        for p in path:
                            path_list.append(
                                copy_results_and_return_path_item(
                                    p, name, s, data_root, slice_to_dir(
                                        s) if create_dir else None))
                    else:
                        path_list.append(copy_results_and_return_path_item(
                            path, name, s, data_root, slice_to_dir(
                                s) if create_dir else None))
        else:
            for s, path in enumerate(value):
                path_list.append(copy_results_and_return_path_item(
                    path, name, s, data_root))
    elif sign.type in [Dict[str, str], Dict[str, Path]]:
        os.makedirs(data_root + '/outputs/artifacts/' + name, exist_ok=True)
        for s, path in value.items():
            path_list.append(copy_results_and_return_path_item(
                path, name, s, data_root))
    elif sign.type in [NestedDict[str], NestedDict[Path]]:
        os.makedirs(data_root + '/outputs/artifacts/' + name, exist_ok=True)
        for s, path in flatten(value).items():
            path_list.append(copy_results_and_return_path_item(
                path, name, s, data_root))

    os.makedirs(data_root + "/outputs/artifacts/%s/%s" % (name, config[
        "catalog_dir_name"]), exist_ok=True)
    with open(data_root + "/outputs/artifacts/%s/%s/%s" % (name, config[
            "catalog_dir_name"], uuid.uuid4()), "w") as f:
        f.write(jsonpickle.dumps({"path_list": path_list}))
    if config["detect_empty_dir"]:
        handle_empty_dir(data_root + "/outputs/artifacts/%s" % name)
    if config["save_path_as_parameter"]:
        with open(data_root + '/outputs/parameters/dflow_%s_path_list'
                  % name, 'w') as f:
            f.write(jsonpickle.dumps(path_list))


def handle_output_parameter(name, value, sign, slices=None, data_root="/tmp"):
    if slices is not None:
        if isinstance(slices, list):
            assert isinstance(value, list) and len(slices) == len(value)
            res = [{"dflow_list_item": v, "order": s}
                   for v, s in zip(value, slices)]
        else:
            res = [{"dflow_list_item": value, "order": slices}]
        with open(data_root + '/outputs/parameters/' + name, 'w') as f:
            f.write(jsonpickle.dumps(res))
    elif isinstance(sign, BigParameter) and config["mode"] != "debug":
        with open(data_root + "/outputs/parameters/" + name, "w") as f:
            f.write(jsonpickle.dumps(value))
    else:
        if isinstance(sign, (Parameter, BigParameter)):
            sign = sign.type
        if sign == str:
            with open(data_root + '/outputs/parameters/' + name, 'w') as f:
                f.write(value)
        else:
            with open(data_root + '/outputs/parameters/' + name, 'w') as f:
                f.write(jsonpickle.dumps(value))


def copy_results_and_return_path_item(path, name, order, data_root="/tmp",
                                      slice_dir=None):
    if path and os.path.exists(str(path)):
        return {"dflow_list_item": copy_results(
                    path, name, data_root, slice_dir), "order": order}
    else:
        return {"dflow_list_item": None, "order": order}


def copy_results(source, name, data_root="/tmp", slice_dir=None):
    source = str(source)
    # if refer to input artifact
    if source.find(data_root + "/inputs/artifacts/") == 0:
        # retain original directory structure
        i = source.find("/", len(data_root + "/inputs/artifacts/"))
        if i == -1:
            rel_path = randstr()
        else:
            rel_path = source[i+1:]
        if slice_dir is not None:
            rel_path = "%s/%s" % (slice_dir, rel_path)
        target = data_root + "/outputs/artifacts/%s/%s" % (name, rel_path)
        copy_file(source, target, shutil.copy)
        if rel_path[:1] == "/":
            rel_path = rel_path[1:]
        return rel_path
    else:
        cwd = os.getcwd()
        if not cwd.endswith("/"):
            cwd = cwd + "/"
        if source.startswith(cwd):
            source = source[len(cwd):]
        rel_path = source[1:] if source[:1] == "/" else source
        if slice_dir is not None:
            rel_path = "%s/%s" % (slice_dir, rel_path)
        target = data_root + "/outputs/artifacts/%s/%s" % (name, rel_path)
        copy_file(source, target)
        return rel_path


def handle_empty_dir(path):
    # touch an empty file in each empty dir, as object storage will ignore
    # empty dirs
    for dn, ds, fs in os.walk(path, followlinks=True):
        if len(ds) == 0 and len(fs) == 0:
            with open(os.path.join(dn, ".empty_dir"), "w"):
                pass


def handle_lineage(wf_name, pod_name, op_obj, input_urns, workflow_urn,
                   data_root="/tmp"):
    task_name = wf_name + "/" + pod_name
    output_sign = op_obj.get_output_sign()
    output_uris = {}
    for name, sign in output_sign.items():
        if isinstance(sign, Artifact):
            output_uris[name] = op_obj.get_output_artifact_storage_key(name)
    input_urns = {name: jsonpickle.loads(urn) if urn[:1] == "["
                  else urn for name, urn in input_urns.items()}
    output_urns = config["lineage"].register_task(
        task_name, input_urns, output_uris, workflow_urn)
    for name, urn in output_urns.items():
        with open("%s/outputs/parameters/dflow_%s_urn" % (data_root, name),
                  "w") as f:
            f.write(urn)


def absolutize(path):
    if path is None:
        return None
    if isinstance(path, str):
        return os.path.abspath(path)
    if isinstance(path, Path):
        return path.absolute()
    if isinstance(path, list):
        return [absolutize(p) for p in path]
    if isinstance(path, dict):
        return {k: absolutize(p) for k, p in path.items()}


def sigalrm_handler(signum, frame):
    raise TimeoutError("Timeout")


def try_to_execute(input, slice_dir, op_obj, output_sign, cwd, timeout=None):
    os.chdir(cwd)
    if slice_dir is not None:
        os.makedirs(slice_dir, exist_ok=True)
        os.chdir(slice_dir)
    if timeout is not None:
        signal.signal(signal.SIGALRM, sigalrm_handler)
        signal.alarm(timeout)
    try:
        output = op_obj.execute(input)
        for n, s in output_sign.items():
            if isinstance(s, Artifact):
                output[n] = absolutize(output[n])
        os.chdir(cwd)
        return output, None
    except Exception as e:
        traceback.print_exc()
        os.chdir(cwd)
        return None, e
    finally:
        if timeout is not None:
            signal.alarm(0)


def get_input_slices(name, data_root="/tmp"):
    art_path = '%s/inputs/artifacts/%s' % (data_root, name)
    catalog = catalog_of_local_artifact(art_path)
    slices = [item["order"] for item in catalog]
    slices.sort()
    return slices
