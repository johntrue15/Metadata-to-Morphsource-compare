# Deployment Guide for Repository Owner

## Quick Start: Enable GitHub Pages

Follow these steps to deploy the MorphoSource AI Assistant:

### Step 1: Enable GitHub Pages

1. Go to your repository on GitHub
2. Click **Settings** (top menu)
3. Scroll down to **Pages** (left sidebar)
4. Under **Source**, select:
   - Source: **GitHub Actions**
5. Click **Save**

That's it! The site will automatically deploy.

### Step 2: Wait for Deployment

- The deployment workflow will run automatically
- Check the **Actions** tab to see progress
- Deployment typically takes 1-2 minutes
- Look for the "Deploy GitHub Pages" workflow

### Step 3: Access Your Site

Once deployed, your site will be available at:
```
https://johntrue15.github.io/Metadata-to-Morphsource-compare/
```

## Configuration (Optional)

### Add Repository Secrets

While the current implementation works client-side (users provide their own API keys), you can optionally add secrets for future backend implementations:

1. Go to: **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
3. Add these secrets:

**OPENAI_API_KEY** (Optional)
- For future backend implementations
- Current version doesn't need this
- Users provide their own keys

**MORPHOSOURCE_API_KEY** (Already configured)
- Used by the comparison workflow
- Keep this for the existing functionality

## Verification Steps

After deployment, verify:

### 1. Site Loads
- [ ] Visit https://johntrue15.github.io/Metadata-to-Morphsource-compare/
- [ ] Page loads without errors
- [ ] Interface displays correctly

### 2. Basic Functionality
- [ ] API key prompt appears
- [ ] Can enter test API key
- [ ] Example prompts are visible
- [ ] Interface is responsive

### 3. Check Deployment Status
```bash
# Via GitHub CLI (if installed)
gh workflow view "Deploy GitHub Pages"

# Or check via web
# Go to: https://github.com/johntrue15/Metadata-to-Morphsource-compare/actions
```

## Testing the Deployed Site

### With Your Own OpenAI API Key

1. Get an API key from: https://platform.openai.com/api-keys
2. Visit your deployed site
3. Enter the API key when prompted
4. Try the example query: "Tell me about lizards on MorphoSource"
5. Verify:
   - Thinking indicator appears
   - Tool call is displayed
   - Response is generated

## Troubleshooting Deployment

### Issue: Workflow Doesn't Run

**Check:**
1. GitHub Pages is enabled in Settings
2. Source is set to "GitHub Actions"
3. The workflow file exists at `.github/workflows/deploy-pages.yml`

**Solution:**
- Manually trigger the workflow from the Actions tab
- Check workflow permissions in Settings → Actions → General

### Issue: 404 Error

**Possible causes:**
1. Deployment hasn't completed yet (wait 2-3 minutes)
2. GitHub Pages not enabled
3. Wrong URL

**Solution:**
- Check Actions tab for deployment status
- Verify Pages settings
- Use correct URL format

### Issue: Blank Page

**Possible causes:**
1. JavaScript errors
2. Browser compatibility
3. Cached old version

**Solution:**
- Check browser console (F12)
- Try different browser
- Hard refresh (Ctrl+Shift+R)

## Updating the Site

Any changes pushed to the `main` branch in the `docs/` folder will automatically trigger redeployment:

```bash
# Make changes to docs/index.html or other files
git add docs/
git commit -m "Update website"
git push origin main
```

The site will redeploy automatically within 2-3 minutes.

## Custom Domain (Optional)

To use a custom domain:

1. Add a CNAME file in the `docs/` folder:
   ```bash
   echo "your-domain.com" > docs/CNAME
   ```

2. Configure your DNS:
   - Add CNAME record pointing to: `johntrue15.github.io`
   - Wait for DNS propagation (up to 24 hours)

3. GitHub Pages will automatically configure HTTPS

## Monitoring

### Check Deployment Logs

1. Go to **Actions** tab
2. Click on latest "Deploy GitHub Pages" workflow
3. View logs for any errors

### Check Site Analytics (Optional)

GitHub Pages doesn't provide analytics by default, but you can add:
- Google Analytics
- Plausible Analytics
- Simple Analytics

Add the tracking code to `docs/index.html` in the `<head>` section.

## Rollback

If you need to rollback to a previous version:

```bash
# Find the commit you want to revert to
git log --oneline

# Revert to that commit
git revert <commit-hash>

# Push changes
git push origin main
```

The site will automatically redeploy with the old version.

## Cost

**GitHub Pages:**
- ✅ Free for public repositories
- ✅ 100GB bandwidth per month
- ✅ Unlimited builds

**OpenAI API:**
- Users pay for their own usage
- No cost to repository owner
- Typical cost: $0.01-0.05 per query

## Support

If you encounter issues:

1. Review [GitHub Pages documentation](https://docs.github.com/en/pages)
2. Check deployment logs in Actions tab
3. Review browser console for errors
4. Check the [Query System Guide](docs/QUERY_SYSTEM_GUIDE.md) for usage instructions

## Next Steps

After successful deployment:

1. ✅ Test the site thoroughly
2. ✅ Share the URL with users
3. ✅ Update documentation if needed
4. ✅ Monitor for any issues
5. ✅ Consider adding analytics (optional)
6. ✅ Set up custom domain (optional)

## Maintenance

**Regular tasks:**
- Check for security updates
- Monitor user feedback
- Update documentation as needed
- Review API usage patterns

**The site is now live and ready to use!** 🎉
