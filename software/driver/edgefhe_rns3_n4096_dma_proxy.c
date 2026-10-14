// SPDX-License-Identifier: GPL-2.0
#include <linux/completion.h>
#include <linux/clk.h>
#include <linux/kref.h>
#include <linux/dma-mapping.h>
#include <linux/dmaengine.h>
#include <linux/fs.h>
#include <linux/io.h>
#include <linux/jiffies.h>
#include <linux/ktime.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/uaccess.h>

#define EDGEFHE_WORDS 4096
#define EDGEFHE_MAX_INPUT_WORDS (3 * EDGEFHE_WORDS)
#define EDGEFHE_WIDE_BYTES 16
#define EDGEFHE_INPUT_BYTES (EDGEFHE_MAX_INPUT_WORDS * EDGEFHE_WIDE_BYTES)
#define EDGEFHE_OUTPUT_BYTES (EDGEFHE_WORDS * EDGEFHE_WIDE_BYTES)
#define EDGEFHE_MAX_TIMEOUT_MS 30000

#define EDGEFHE_IOC_RUN \
	_IOWR('E', 0x04, struct edgefhe_rns3_n4096_dma_request)

#define NTT_AP_CTRL 0x00
#define NTT_OPERATION 0x10
#define NTT_AP_START 0x01
#define NTT_AP_IDLE 0x04

/*
 * The Linux ioctl size field is only 14 bits. Keep bulk arrays outside this
 * descriptor and transfer them through explicit 64-bit userspace pointers.
 */
struct edgefhe_rns3_n4096_dma_request {
	u32 operation;
	u32 reserved0;
	u32 input_words;
	u32 output_words;
	u64 input_ptr;
	u64 output_ptr;
	u32 timeout_ms;
	u32 elapsed_us;
	s32 status;
	u32 reserved;
};

struct edgefhe_proxy {
	struct device *dev;
	struct kref refs;
	struct clk *ap_clk;
	bool online;
	bool poisoned;
	struct dma_chan *tx_chan;
	struct dma_chan *rx_chan;
	struct miscdevice miscdev;
	struct mutex lock;
	void __iomem *ntt_regs;
	void *tx_buf;
	void *rx_buf;
	dma_addr_t tx_dma;
	dma_addr_t rx_dma;
};

static void edgefhe_wipe_buffers(struct edgefhe_proxy *proxy)
{
	memzero_explicit(proxy->tx_buf, EDGEFHE_INPUT_BYTES);
	memzero_explicit(proxy->rx_buf, EDGEFHE_OUTPUT_BYTES);
}

struct edgefhe_dma_done {
	struct completion completion;
	enum dmaengine_tx_result result;
};

static void edgefhe_dma_callback(
	void *arg, const struct dmaengine_result *result)
{
	struct edgefhe_dma_done *done = arg;

	done->result = result ? result->result : DMA_TRANS_NOERROR;
	complete(&done->completion);
}

static int edgefhe_parse_ntt_control(
	struct platform_device *pdev, struct edgefhe_proxy *proxy)
{
	struct resource *res = platform_get_resource(pdev, IORESOURCE_MEM, 0);

	if (!res || res->start != 0xa0010000 || resource_size(res) != 0x10000)
		return -EINVAL;
	proxy->ntt_regs = devm_ioremap_resource(&pdev->dev, res);
	return PTR_ERR_OR_ZERO(proxy->ntt_regs);
}

static int edgefhe_submit_transfer(
	struct edgefhe_proxy *proxy,
	struct edgefhe_rns3_n4096_dma_request *req)
{
	struct dma_async_tx_descriptor *rx_desc;
	struct dma_async_tx_descriptor *tx_desc;
	struct edgefhe_dma_done rx_done;
	struct edgefhe_dma_done tx_done;
	void __user *input_user = u64_to_user_ptr(req->input_ptr);
	void __user *output_user = u64_to_user_ptr(req->output_ptr);
	dma_cookie_t rx_cookie;
	dma_cookie_t tx_cookie;
	unsigned long timeout;
	unsigned long deadline;
	unsigned long sampled_jiffies;
	unsigned int beat;
	u32 *values;
	ktime_t start;
	ktime_t end;
	u32 expected_input_words;
	u32 tx_bytes;
	u32 rx_bytes;
	u32 ap_ctrl;
	int ret = 0;

	if (!proxy->online)
		return -ENODEV;
	if (proxy->poisoned)
		return -EIO;
	if (req->reserved0 || req->reserved)
		return -EINVAL;
	if (req->timeout_ms > EDGEFHE_MAX_TIMEOUT_MS)
		return -EINVAL;
	if (req->operation > 5)
		return -EINVAL;
	switch (req->operation) {
	case 2:
	case 3:
	case 5:
		expected_input_words = 2 * EDGEFHE_WORDS;
		break;
	case 4:
		expected_input_words = 3 * EDGEFHE_WORDS;
		break;
	default:
		expected_input_words = EDGEFHE_WORDS;
		break;
	}
	if (req->input_words != expected_input_words ||
	    req->output_words != EDGEFHE_WORDS)
		return -EINVAL;
	if (!input_user || !output_user)
		return -EFAULT;

	tx_bytes = req->input_words * EDGEFHE_WIDE_BYTES;
	rx_bytes = req->output_words * EDGEFHE_WIDE_BYTES;
	if (copy_from_user(proxy->tx_buf, input_user, tx_bytes)) {
		ret = -EFAULT;
		goto wipe;
	}
	/* HLS Montgomery bounds require canonical residues and zero padding. */
	values = proxy->tx_buf;
	for (beat = 0; beat < req->input_words; beat++) {
		if (values[4 * beat] >= 998244353U ||
		    values[4 * beat + 1] >= 1004535809U ||
		    values[4 * beat + 2] >= 469762049U ||
		    values[4 * beat + 3] != 0) {
			ret = -EINVAL;
			goto wipe;
		}
	}
	memset(proxy->rx_buf, 0, rx_bytes);

	ap_ctrl = ioread32(proxy->ntt_regs + NTT_AP_CTRL);
	if ((ap_ctrl & NTT_AP_IDLE) == 0) {
		ret = -EBUSY;
		goto wipe;
	}

	iowrite32(req->operation & 0x7, proxy->ntt_regs + NTT_OPERATION);
	init_completion(&rx_done.completion);
	init_completion(&tx_done.completion);
	rx_done.result = DMA_TRANS_NOERROR;
	tx_done.result = DMA_TRANS_NOERROR;

	rx_desc = dmaengine_prep_slave_single(
		proxy->rx_chan, proxy->rx_dma, rx_bytes, DMA_DEV_TO_MEM,
		DMA_PREP_INTERRUPT | DMA_CTRL_ACK);
	if (!rx_desc) {
		ret = -EIO;
		goto wipe;
	}
	rx_desc->callback_result = edgefhe_dma_callback;
	rx_desc->callback_param = &rx_done;
	rx_cookie = dmaengine_submit(rx_desc);
	ret = dma_submit_error(rx_cookie);
	if (ret)
		goto terminate;

	tx_desc = dmaengine_prep_slave_single(
		proxy->tx_chan, proxy->tx_dma, tx_bytes, DMA_MEM_TO_DEV,
		DMA_PREP_INTERRUPT | DMA_CTRL_ACK);
	if (!tx_desc) {
		ret = -EIO;
		goto terminate;
	}
	tx_desc->callback_result = edgefhe_dma_callback;
	tx_desc->callback_param = &tx_done;

	tx_cookie = dmaengine_submit(tx_desc);
	ret = dma_submit_error(tx_cookie);
	if (ret)
		goto terminate;

	timeout = msecs_to_jiffies(req->timeout_ms ? req->timeout_ms : 5000);
	deadline = jiffies + timeout;
	dma_wmb();
	start = ktime_get();
	dma_async_issue_pending(proxy->rx_chan);
	dma_async_issue_pending(proxy->tx_chan);
	iowrite32(NTT_AP_START, proxy->ntt_regs + NTT_AP_CTRL);

	if (!wait_for_completion_timeout(&rx_done.completion, timeout)) {
		ret = -ETIMEDOUT;
		goto terminate;
	}
	/* A completed TX needs no remaining deadline; otherwise use one budget. */
	sampled_jiffies = jiffies;
	if (!completion_done(&tx_done.completion) &&
	    (time_after_eq(sampled_jiffies, deadline) ||
	     !wait_for_completion_timeout(&tx_done.completion, deadline - sampled_jiffies))) {
		ret = -ETIMEDOUT;
		goto terminate;
	}
	if (rx_done.result != DMA_TRANS_NOERROR ||
	    tx_done.result != DMA_TRANS_NOERROR) {
		ret = -EIO;
		goto terminate;
	}

	dma_rmb();
	end = ktime_get();
	req->elapsed_us = (u32)ktime_to_us(ktime_sub(end, start));
	if (copy_to_user(output_user, proxy->rx_buf, rx_bytes))
		ret = -EFAULT;

terminate:
	if (ret) {
		/* DMA reset cannot reset the HLS pipeline. Fail closed until PL reload. */
		proxy->poisoned = true;
		dmaengine_terminate_sync(proxy->rx_chan);
		dmaengine_terminate_sync(proxy->tx_chan);
	}
wipe:
	edgefhe_wipe_buffers(proxy);
	req->status = ret;
	return ret;
}

static long edgefhe_ioctl(
	struct file *file, unsigned int cmd, unsigned long arg)
{
	struct edgefhe_proxy *proxy = file->private_data;
	struct edgefhe_rns3_n4096_dma_request req;
	int ret;

	if (cmd != EDGEFHE_IOC_RUN)
		return -ENOTTY;
	if (copy_from_user(&req, (void __user *)arg, sizeof(req)))
		return -EFAULT;

	req.elapsed_us = 0;
	req.status = 0;
	mutex_lock(&proxy->lock);
	ret = edgefhe_submit_transfer(proxy, &req);
	req.status = ret;
	mutex_unlock(&proxy->lock);

	if (copy_to_user((void __user *)arg, &req, sizeof(req)) && ret == 0)
		ret = -EFAULT;
	return ret;
}

static void edgefhe_free(struct kref *refs)
{
	struct edgefhe_proxy *proxy = container_of(refs, struct edgefhe_proxy, refs);
	kfree(proxy);
}

static int edgefhe_open(struct inode *inode, struct file *file)
{
	struct miscdevice *miscdev = file->private_data;
	struct edgefhe_proxy *proxy = container_of(miscdev, struct edgefhe_proxy, miscdev);
	int ret = 0;

	mutex_lock(&proxy->lock);
	if (!proxy->online)
		ret = -ENODEV;
	else {
		kref_get(&proxy->refs);
		file->private_data = proxy;
	}
	mutex_unlock(&proxy->lock);
	return ret;
}

static int edgefhe_release(struct inode *inode, struct file *file)
{
	struct edgefhe_proxy *proxy = file->private_data;
	kref_put(&proxy->refs, edgefhe_free);
	return 0;
}

static const struct file_operations edgefhe_fops = {
	.owner = THIS_MODULE,
	.open = edgefhe_open,
	.release = edgefhe_release,
	.unlocked_ioctl = edgefhe_ioctl,
	.compat_ioctl = edgefhe_ioctl,
};

static int edgefhe_probe(struct platform_device *pdev)
{
	struct edgefhe_proxy *proxy;
	int ret;

	BUILD_BUG_ON(sizeof(struct edgefhe_rns3_n4096_dma_request) != 48);
	BUILD_BUG_ON(offsetof(struct edgefhe_rns3_n4096_dma_request, input_ptr) != 16);
	BUILD_BUG_ON(offsetof(struct edgefhe_rns3_n4096_dma_request, status) != 40);
	proxy = kzalloc(sizeof(*proxy), GFP_KERNEL);
	if (!proxy)
		return -ENOMEM;
	proxy->dev = &pdev->dev;
	kref_init(&proxy->refs);
	mutex_init(&proxy->lock);
	platform_set_drvdata(pdev, proxy);

	/* Negotiate the 32-bit AXI DMA capability. Linux may reject a 31-bit
	 * mask despite low CMA. The full allocation endpoints are checked
	 * below against the narrower routed low-DDR aperture before exposure.
	 */
	ret = dma_set_mask_and_coherent(&pdev->dev, DMA_BIT_MASK(32));
	if (ret) {
		dev_err_probe(&pdev->dev, ret, "failed to set DMA mask\n");
		goto free_proxy;
	}
	ret = edgefhe_parse_ntt_control(pdev, proxy);
	if (ret)
		goto free_proxy;
	if (of_find_property(pdev->dev.of_node, "dma-control", NULL)) {
		ret = -EINVAL;
		goto free_proxy;
	}
	proxy->ap_clk = devm_clk_get(&pdev->dev, "ap_clk");
	if (IS_ERR(proxy->ap_clk)) {
		ret = PTR_ERR(proxy->ap_clk);
		goto free_proxy;
	}
	ret = clk_prepare_enable(proxy->ap_clk);
	if (ret)
		goto free_proxy;
	ret = clk_rate_exclusive_get(proxy->ap_clk);
	if (ret)
		goto disable_clock;
	if (clk_get_rate(proxy->ap_clk) < 99900000 ||
	    clk_get_rate(proxy->ap_clk) > 100000000) {
		ret = -ERANGE;
		goto release_clock;
	}

	proxy->tx_chan = dma_request_chan(&pdev->dev, "tx");
	if (IS_ERR(proxy->tx_chan)) {
		ret = dev_err_probe(
			&pdev->dev, PTR_ERR(proxy->tx_chan),
			"failed to request tx DMA channel\n");
		proxy->tx_chan = NULL;
		goto release_clock;
	}
	proxy->rx_chan = dma_request_chan(&pdev->dev, "rx");
	if (IS_ERR(proxy->rx_chan)) {
		ret = dev_err_probe(
			&pdev->dev, PTR_ERR(proxy->rx_chan),
			"failed to request rx DMA channel\n");
		proxy->rx_chan = NULL;
		goto release_channels;
	}

	proxy->tx_buf = dma_alloc_coherent(
		&pdev->dev, EDGEFHE_INPUT_BYTES, &proxy->tx_dma, GFP_KERNEL);
	if (!proxy->tx_buf) {
		ret = -ENOMEM;
		dev_err_probe(&pdev->dev, ret, "TX coherent allocation failed\n");
		goto release_channels;
	}
	proxy->rx_buf = dma_alloc_coherent(
		&pdev->dev, EDGEFHE_OUTPUT_BYTES, &proxy->rx_dma, GFP_KERNEL);
	if (!proxy->rx_buf) {
		ret = -ENOMEM;
		dev_err_probe(&pdev->dev, ret, "RX coherent allocation failed\n");
		goto free_tx;
	}
	/* Both ends must fit the routed DDR-low aperture, even with a mask. */
	if ((proxy->tx_dma & 15) || (proxy->rx_dma & 15) ||
	    (u64)proxy->tx_dma + EDGEFHE_INPUT_BYTES > 0x80000000ULL ||
	    (u64)proxy->rx_dma + EDGEFHE_OUTPUT_BYTES > 0x80000000ULL) {
		ret = -ERANGE;
		dev_err(&pdev->dev, "DMA buffers outside aligned low-DDR aperture: "
			"tx=%pad rx=%pad\n", &proxy->tx_dma, &proxy->rx_dma);
		goto free_rx;
	}
	proxy->online = true;
	proxy->miscdev.minor = MISC_DYNAMIC_MINOR;
	proxy->miscdev.name = "edgefhe_rns3_n4096_dma";
	proxy->miscdev.fops = &edgefhe_fops;
	proxy->miscdev.parent = &pdev->dev;
	ret = misc_register(&proxy->miscdev);
	if (ret)
		goto free_rx;

	dev_info(
		&pdev->dev,
		"EdgeFHE RNS3 parallel N=4096 DMA proxy ready: "
		"mode=dmaengine-irq ii1-guarded-v3 tx=%pad rx=%pad\n",
		&proxy->tx_dma, &proxy->rx_dma);
	return 0;

free_rx:
	dma_free_coherent(
		&pdev->dev, EDGEFHE_OUTPUT_BYTES, proxy->rx_buf, proxy->rx_dma);
free_tx:
	dma_free_coherent(
		&pdev->dev, EDGEFHE_INPUT_BYTES, proxy->tx_buf, proxy->tx_dma);
release_channels:
	if (proxy->rx_chan)
		dma_release_channel(proxy->rx_chan);
	if (proxy->tx_chan)
		dma_release_channel(proxy->tx_chan);
release_clock:
	clk_rate_exclusive_put(proxy->ap_clk);
disable_clock:
	clk_disable_unprepare(proxy->ap_clk);
free_proxy:
	kref_put(&proxy->refs, edgefhe_free);
	return ret;
}

static int edgefhe_remove(struct platform_device *pdev)
{
	struct edgefhe_proxy *proxy = platform_get_drvdata(pdev);

	misc_deregister(&proxy->miscdev);
	mutex_lock(&proxy->lock);
	proxy->online = false;
	dmaengine_terminate_sync(proxy->rx_chan);
	dmaengine_terminate_sync(proxy->tx_chan);
	edgefhe_wipe_buffers(proxy);
	dma_free_coherent(
		&pdev->dev, EDGEFHE_OUTPUT_BYTES, proxy->rx_buf, proxy->rx_dma);
	dma_free_coherent(
		&pdev->dev, EDGEFHE_INPUT_BYTES, proxy->tx_buf, proxy->tx_dma);
	if (proxy->rx_chan)
		dma_release_channel(proxy->rx_chan);
	if (proxy->tx_chan)
		dma_release_channel(proxy->tx_chan);
	clk_rate_exclusive_put(proxy->ap_clk);
	clk_disable_unprepare(proxy->ap_clk);
	mutex_unlock(&proxy->lock);
	kref_put(&proxy->refs, edgefhe_free);
	return 0;
}

static const struct of_device_id edgefhe_of_match[] = {
	{ .compatible = "ncue,edgefhe-rns3-n4096-ii1-dma-proxy" },
	{ }
};
MODULE_DEVICE_TABLE(of, edgefhe_of_match);

static struct platform_driver edgefhe_driver = {
	.probe = edgefhe_probe,
	.remove = edgefhe_remove,
	.driver = {
		.name = "edgefhe-rns3-n4096-dma-proxy",
		.of_match_table = edgefhe_of_match,
	},
};

module_platform_driver(edgefhe_driver);

MODULE_AUTHOR("NCUE EdgeFHE Project");
MODULE_DESCRIPTION("DMA proxy for the EdgeFHE RNS3 parallel N=4096 accelerator");
MODULE_LICENSE("GPL");
MODULE_VERSION("ii1-guarded-v3");
