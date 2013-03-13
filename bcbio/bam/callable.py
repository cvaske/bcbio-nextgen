"""Examine callable regions following genome mapping of short reads.

Identifies callable analysis regions surrounded by larger regions lacking
aligned bases. This allows parallelization of smaller chromosome chunks
through post-processing and variant calling, with each sub-section
mapping handled separately.
"""
import contextlib
import os
import shutil

import pybedtools
import pysam

from bcbio import utils, broad
from bcbio.log import logger
from bcbio.distributed import messaging
from bcbio.distributed.messaging import parallel_runner
from bcbio.distributed.split import parallel_split_combine
from bcbio.distributed.transaction import file_transaction
from bcbio.pipeline import shared

def parallel_callable_loci(in_bam, ref_file, config):
    num_cores = config["algorithm"].get("num_cores", 1)
    data = {"work_bam": in_bam, "sam_ref": ref_file, "config": config}
    if num_cores > 1:
        parallel = {"type": "local", "cores": num_cores, "module": "bcbio.distributed"}
        runner = parallel_runner(parallel, {}, config)
        split_fn = shared.process_bam_by_chromosome("-callable.bed", "work_bam")
        out = parallel_split_combine([[data]], split_fn, runner,
                                      "calc_callable_loci", "combine_bed",
                                      "callable_bed", ["config"])[0]
    else:
        out = calc_callable_loci(data)
    return out[0]["callable_bed"]

def combine_bed(in_files, out_file, config):
    """Combine multiple BED files into a single output.
    """
    if not utils.file_exists(out_file):
        with file_transaction(out_file) as tx_out_file:
            with open(tx_out_file, "w") as out_handle:
                for in_file in in_files:
                    with open(in_file) as in_handle:
                        shutil.copyfileobj(in_handle, out_handle)
    return out_file

def calc_callable_loci(data, region=None, out_file=None):
    """Determine callable bases for input BAM using Broad's CallableLoci walker.

    http://www.broadinstitute.org/gatk/gatkdocs/
    org_broadinstitute_sting_gatk_walkers_coverage_CallableLoci.html
    """
    broad_runner = broad.runner_from_config(data["config"])
    if out_file is None:
        out_file = "%s-callable.bed" % os.path.splitext(data["work_bam"])[0]
    out_summary = "%s-callable-summary.txt" % os.path.splitext(data["work_bam"])[0]
    variant_regions = data["config"]["algorithm"].get("variant_regions", None)
    if not utils.file_exists(out_file):
        with file_transaction(out_file) as tx_out_file:
            broad_runner.run_fn("picard_index", data["work_bam"])
            params = ["-T", "CallableLoci",
                      "-R", data["sam_ref"],
                      "-I", data["work_bam"],
                      "--out", tx_out_file,
                      "--summary", out_summary]
            region = shared.subset_variant_regions(variant_regions, region, tx_out_file)
            if region:
                params += ["-L", region]
            broad_runner.run_gatk(params)
    return [{"callable_bed": out_file, "config": data["config"]}]

def get_ref_bedtool(ref_file, config):
    """Retrieve a pybedtool BedTool object with reference sizes from input reference.
    """
    broad_runner = broad.runner_from_config(config)
    ref_dict = broad_runner.run_fn("picard_index_ref", ref_file)
    ref_lines = []
    with contextlib.closing(pysam.Samfile(ref_dict, "r")) as ref_sam:
        for sq in ref_sam.header["SQ"]:
            ref_lines.append("%s\t%s\t%s" % (sq["SN"], 0, sq["LN"]))
    return pybedtools.BedTool("\n".join(ref_lines), from_string=True)

def _get_nblock_regions(in_file, min_n_size):
    """Retrieve coordinates of regions in reference genome with no mapping.
    These are potential breakpoints for parallelizing analysis.
    """
    out_lines = []
    with open(in_file) as in_handle:
        for line in in_handle:
            contig, start, end, ctype = line.rstrip().split()
            if (ctype in ["REF_N", "NO_COVERAGE"] and
                  int(end) - int(start) > min_n_size):
                out_lines.append("%s\t%s\t%s\n" % (contig, start, end))
    return pybedtools.BedTool("\n".join(out_lines), from_string=True)

def _add_config_regions(nblock_regions, ref_regions, config):
    """Add additional nblock regions based on configured regions to call.
    Identifies user defined regions which we should not be analyzing.
    """
    input_regions_bed = config["algorithm"].get("variant_regions", None)
    if input_regions_bed:
        input_regions = pybedtools.BedTool(input_regions_bed)
        input_nblock = ref_regions.subtract(nblock_regions)
        return nblock_regions.merge(input_nblock)
    else:
        return nblock_regions

def block_regions(in_bam, ref_file, config):
    """Find blocks of regions for analysis from mapped input BAM file.

    Identifies islands of callable regions, surrounding by regions
    with no read support, that can be analyzed independently.
    """
    min_n_size = int(config["algorithm"].get("nomap_split_size", 2000))
    callable_bed = parallel_callable_loci(in_bam, ref_file, config)
    ref_regions = get_ref_bedtool(ref_file, config)
    nblock_regions = _get_nblock_regions(callable_bed, min_n_size)
    nblock_regions = _add_config_regions(nblock_regions, ref_regions, config)
    return [(r.chrom, int(r.start), int(r.stop)) for r in ref_regions.subtract(nblock_regions)]

def _write_bed_regions(sample, final_regions):
    work_dir = sample["dirs"]["work"]
    ref_regions = get_ref_bedtool(sample["sam_ref"], sample["config"])
    noanalysis_regions = ref_regions.subtract(final_regions)
    out_file = os.path.join(work_dir, "analysis_blocks.bed")
    out_file_ref = os.path.join(work_dir, "noanalysis_blocks.bed")
    final_regions.saveas(out_file)
    noanalysis_regions.saveas(out_file_ref)
    return out_file_ref

def combine_sample_regions(samples):
    """Combine islands of callable regions from multiple samples.
    Creates a global set of callable samples usable across a
    project with multi-sample calling.
    """
    min_n_size = int(samples[0]["config"]["algorithm"].get("nomap_split_size", 2000))
    final_regions = None
    for regions in (x["regions"] for x in samples):
        bed_lines = ["%s\t%s\t%s" % (c, s, e) for (c, s, e) in regions]
        bed_regions = pybedtools.BedTool("\n".join(bed_lines), from_string=True)
        if final_regions is None:
            final_regions = bed_regions
        else:
            final_regions = final_regions.merge(bed_regions, d=min_n_size)
    no_analysis_file = _write_bed_regions(samples[0], final_regions)
    regions = {"analysis": [(r.chrom, int(r.start), int(r.stop)) for r in final_regions],
               "noanalysis": no_analysis_file}
    out = []
    for s in samples:
        s["regions"] = regions
        out.append([s])
    return out
